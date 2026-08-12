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

RUNTIME_DISTRIBUTIONS = (
    "anyio",
    "certifi",
    "charset-normalizer",
    "click",
    "colorama",
    "fastembed",
    "filelock",
    "flatbuffers",
    "fsspec",
    "h11",
    "hf-xet",
    "httpcore",
    "httpx",
    "huggingface-hub",
    "idna",
    "loguru",
    "mmh3",
    "mpmath",
    "numpy",
    "onnxruntime",
    "packaging",
    "pillow",
    "protobuf",
    "py-rust-stemmers",
    "PySide6",
    "PySide6-Addons",
    "PySide6-Essentials",
    "pywin32",
    "PyYAML",
    "requests",
    "shiboken6",
    "sympy",
    "tokenizers",
    "tqdm",
    "typing-extensions",
    "urllib3",
    "win32-setctime",
)

datas = [
    (
        str(source_root / "amadeus_desktop" / "resources" / "app_icon"),
        "amadeus_desktop/resources/app_icon",
    ),
    (
        str(source_root / "amadeus_desktop" / "resources" / "builtin_pet"),
        "amadeus_desktop/resources/builtin_pet",
    ),
    (
        str(source_root / "amadeus_desktop" / "resources" / "licenses"),
        "amadeus_desktop/resources/licenses",
    ),
    (
        str(source_root / "amadeus_desktop" / "resources" / "provider_catalog"),
        "amadeus_desktop/resources/provider_catalog",
    ),
]
binaries = []
hiddenimports = ["pywintypes", "win32api", "win32cred", "win32timezone"]

# onnxruntime's standard PyInstaller hook already collects its native runtime.
# collect_all("onnxruntime") would additionally ship quantization tools, test
# datasets, and unrelated ONNX examples, which are outside the desktop runtime.
for package in ("fastembed", "tokenizers"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

for distribution in RUNTIME_DISTRIBUTIONS:
    datas += copy_metadata(distribution)

build_info_value = os.environ.get("AMADEUS_PYINSTALLER_BUILD_INFO", "").strip()
if not build_info_value:
    raise SystemExit("AMADEUS_PYINSTALLER_BUILD_INFO is required")
build_info_path = Path(build_info_value).resolve(strict=True)
if build_info_path.is_symlink() or not build_info_path.is_file():
    raise SystemExit("Configured build-info path is unsafe")
datas.append((str(build_info_path), "amadeus_desktop/resources"))

icon_value = os.environ.get("AMADEUS_PYINSTALLER_ICON", "").strip()
if not icon_value:
    raise SystemExit("AMADEUS_PYINSTALLER_ICON is required")
icon_path = Path(icon_value).resolve(strict=True)
if icon_path.is_symlink() or not icon_path.is_file() or icon_path.suffix.lower() != ".ico":
    raise SystemExit("Configured application icon is unsafe")

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
    excludes=["_pytest", "fsspec.conftest", "pkg_resources", "pytest", "setuptools"],
    noarchive=False,
    optimize=0,
)

# QtGui's broad plugin hook pulls PDF and virtual-keyboard plugins even though
# Amadeus only renders ordinary pet/chat images. Those optional modules are not
# part of the application and have different open-source licensing terms.
forbidden_qt_entries = {
    "pyside6/plugins/imageformats/qpdf.dll",
    "pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    "pyside6/qt6pdf.dll",
    "pyside6/qt6qml.dll",
    "pyside6/qt6qmlmeta.dll",
    "pyside6/qt6qmlmodels.dll",
    "pyside6/qt6qmlworkerscript.dll",
    "pyside6/qt6quick.dll",
    "pyside6/qt6virtualkeyboard.dll",
}
analysis.binaries = [
    entry
    for entry in analysis.binaries
    if entry[0].replace("\\", "/").lower() not in forbidden_qt_entries
]
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
    icon=str(icon_path),
    version=str(desktop_root / "packaging" / "amadeus-version-info.txt"),
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
