from __future__ import annotations

import json
from pathlib import Path

from amadeus_desktop.pet_assets import PetAssetService
from amadeus_desktop.tools.pet_acceptance import InteractionAcceptance
from amadeus_desktop.tools.pet_preview import export_contact_sheets
from amadeus_desktop.ui.pet_window import PetWindow


def test_contact_sheet_export_covers_every_action(qapp, tmp_path: Path) -> None:
    asset = PetAssetService(tmp_path / "pets").load_builtin()

    outputs = export_contact_sheets(asset, tmp_path / "evidence")

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
