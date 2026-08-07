from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

import amadeus_desktop.tools.pet_preview as pet_preview
from amadeus_desktop.pet_assets import PetAssetService, builtin_pet_root
from amadeus_desktop.tools.pet_acceptance import InteractionAcceptance
from amadeus_desktop.tools.pet_preview import PreviewWindow, export_contact_sheets
from amadeus_desktop.ui.pet_window import PetWindow


def test_preview_tools_reuse_sheet_and_honor_logical_frame_size(
    qapp,
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    asset = PetAssetService(tmp_path / "pets").load_builtin()
    load_calls = 0
    original_load = pet_preview._load_spritesheet

    def counting_load(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(pet_preview, "_load_spritesheet", counting_load)

    outputs = export_contact_sheets(asset, tmp_path / "evidence")

    assert load_calls == 1
    assert [path.stem for path in outputs] == [
        "00-idle",
        "01-move_right",
        "02-move_left",
        "03-greeting",
        "04-jump",
        "05-error",
        "06-responding",
        "07-waiting",
        "08-thinking",
    ]
    assert all(path.stat().st_size > 0 for path in outputs)
    first_contact_sheet = QImage(str(outputs[0]))
    spec = asset.manifest.spritesheet
    assert first_contact_sheet.size() == QSize(
        spec.frame_width * len(asset.manifest.animations["idle"].frames),
        spec.frame_height,
    )

    window = PreviewWindow(asset)
    qtbot.addWidget(window)
    window.timer.stop()
    expected_preview_size = QSize(
        spec.logical_frame_width * 2,
        spec.logical_frame_height * 2,
    )

    assert load_calls == 2
    assert window.image.minimumSize() == expected_preview_size
    assert window.image.pixmap() is not None
    assert window.image.pixmap().size() == expected_preview_size

    window._advance()
    window._advance()
    assert load_calls == 2


def test_preview_cli_loads_the_exact_large_builtin_through_its_pinned_path(
    qapp,
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}

    def fake_export(asset, destination):
        captured["asset"] = asset
        captured["destination"] = destination
        return []

    monkeypatch.setattr(pet_preview, "export_contact_sheets", fake_export)
    destination = tmp_path / "preview"

    assert pet_preview.main([str(builtin_pet_root()), "--export-dir", str(destination)]) == 0
    assert captured["asset"].spritesheet_path == builtin_pet_root() / "spritesheet.webp"
    assert captured["destination"] == destination


def test_manual_acceptance_counter_requires_fifty_of_each(qapp, qtbot, tmp_path: Path) -> None:
    asset = PetAssetService(tmp_path / "pets").load_builtin()
    pet = PetWindow(asset)
    result_path = tmp_path / "result.json"
    counter = InteractionAcceptance(pet, result_path)
    qtbot.addWidget(pet)
    qtbot.addWidget(counter)

    for _ in range(50):
        pet.clicked.emit()
    assert counter.phase == "drag"
    assert not result_path.exists()

    for _ in range(50):
        pet.drag_finished.emit(pet.pos())
    document = json.loads(result_path.read_text(encoding="utf-8"))
    assert counter.phase == "complete"
    assert document["passed"] is True
    assert document["clicks"] == 50
    assert document["drags"] == 50


def test_wrong_event_resets_manual_acceptance(qapp, qtbot, tmp_path: Path) -> None:
    asset = PetAssetService(tmp_path / "pets").load_builtin()
    pet = PetWindow(asset)
    counter = InteractionAcceptance(pet, None)
    qtbot.addWidget(pet)
    qtbot.addWidget(counter)

    pet.clicked.emit()
    pet.drag_finished.emit(pet.pos())

    assert counter.phase == "click"
    assert counter.clicks == 0
    assert counter.drags == 0
