# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


desktop_root = Path(SPEC).resolve().parent.parent
source_root = desktop_root / "src"
entry_point = source_root / "amadeus_desktop" / "__main__.py"
sys.path.insert(0, str(source_root))

from amadeus_desktop.embedding_model import MODEL_BUNDLE_FILES

datas = [
    (
        str(source_root / "amadeus_desktop" / "resources" / "builtin_pet"),
        "amadeus_desktop/resources/builtin_pet",
    ),
    (
        str(
            source_root
            / "amadeus_desktop"
            / "resources"
            / "licenses"
            / "P5B_THIRD_PARTY_NOTICES.txt"
        ),
        "amadeus_desktop/resources/licenses",
    ),
]
binaries = []
hiddenimports = []

# onnxruntime's standard PyInstaller hook already collects its native runtime.
# collect_all("onnxruntime") would additionally ship quantization tools, test
# datasets, and unrelated ONNX examples, which are outside the desktop runtime.
for package in ("fastembed", "tokenizers"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

for distribution in ("fastembed", "onnxruntime", "numpy"):
    datas += copy_metadata(distribution)

model_value = os.environ.get("AMADEUS_PYINSTALLER_MODEL_DIR", "").strip()
if model_value:
    model_directory = Path(model_value).resolve(strict=True)
    if not model_directory.is_dir():
        raise SystemExit("Configured embedding model path is not a directory")
    model_entries = tuple(model_directory.iterdir())
    if {entry.name for entry in model_entries} != set(MODEL_BUNDLE_FILES):
        raise SystemExit("Configured embedding model directory contains unexpected files")
    if any(entry.is_symlink() or not entry.is_file() for entry in model_entries):
        raise SystemExit("Configured embedding model directory contains an unsafe entry")
    for model_file in MODEL_BUNDLE_FILES:
        datas.append(
            (
                str(model_directory / model_file),
                "amadeus_desktop/resources/embedding_model",
            )
        )

analysis = Analysis(
    [str(entry_point)],
    pathex=[str(source_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(desktop_root / "packaging" / "pyi_rth_no_cmd_platform.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Amadeus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Amadeus",
)
