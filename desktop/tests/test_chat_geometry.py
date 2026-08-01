from __future__ import annotations

import pytest
from PySide6.QtCore import QRect, QSize

from amadeus_desktop.chat_geometry import (
    CHAT_PANEL_GAP,
    calculate_chat_panel_placement,
    compact_panel_size,
)


def assert_fully_visible(top_left, size: QSize, work_area: QRect) -> None:
    geometry = QRect(top_left, size)
    assert work_area.contains(geometry.topLeft())
    assert work_area.contains(geometry.bottomRight())


def test_right_side_is_preferred_when_both_sides_fit() -> None:
    work_area = QRect(0, 0, 1920, 1040)
    pet = QRect(760, 700, 192, 208)
    panel_size = QSize(380, 560)

    placement = calculate_chat_panel_placement(pet, panel_size, [work_area])

    assert placement.side == "right"
    assert placement.top_left.x() == pet.right() + CHAT_PANEL_GAP + 1
    assert placement.top_left.y() == pet.bottom() - panel_size.height() + 1
    assert_fully_visible(placement.top_left, panel_size, work_area)


def test_left_side_is_used_when_right_side_does_not_fit() -> None:
    work_area = QRect(0, 0, 1920, 1040)
    pet = QRect(1700, 700, 192, 208)
    panel_size = QSize(380, 560)

    placement = calculate_chat_panel_placement(pet, panel_size, [work_area])

    assert placement.side == "left"
    assert placement.top_left.x() == pet.left() - CHAT_PANEL_GAP - panel_size.width()
    assert_fully_visible(placement.top_left, panel_size, work_area)


def test_larger_side_wins_and_is_clamped_when_neither_side_fits() -> None:
    work_area = QRect(0, 0, 800, 600)
    panel_size = QSize(600, 500)

    placement_left = calculate_chat_panel_placement(
        QRect(520, 450, 180, 140), panel_size, [work_area]
    )
    placement_right = calculate_chat_panel_placement(
        QRect(100, 450, 180, 140), panel_size, [work_area]
    )

    assert placement_left.side == "left"
    assert placement_left.top_left.x() == work_area.left()
    assert placement_right.side == "right"
    assert placement_right.top_left.x() == work_area.right() - panel_size.width() + 1
    assert_fully_visible(placement_left.top_left, panel_size, work_area)
    assert_fully_visible(placement_right.top_left, panel_size, work_area)


@pytest.mark.parametrize(
    ("pet", "expected_side", "expected_y"),
    [
        (QRect(10, 10, 100, 100), "right", 0),
        (QRect(890, 10, 100, 100), "left", 0),
        (QRect(10, 690, 100, 100), "right", 390),
        (QRect(890, 690, 100, 100), "left", 390),
    ],
)
def test_panel_is_visible_at_all_four_work_area_corners(
    pet: QRect,
    expected_side: str,
    expected_y: int,
) -> None:
    work_area = QRect(0, 0, 1000, 800)
    size = QSize(300, 400)

    placement = calculate_chat_panel_placement(pet, size, [work_area])

    assert placement.side == expected_side
    assert placement.top_left.y() == expected_y
    assert_fully_visible(placement.top_left, size, work_area)


def test_pet_centre_selects_negative_coordinate_work_area() -> None:
    primary = QRect(0, 0, 1920, 1040)
    left_screen = QRect(-1600, -200, 1600, 900)
    pet = QRect(-1530, 450, 160, 180)
    size = QSize(380, 560)

    placement = calculate_chat_panel_placement(pet, size, [primary, left_screen])

    assert placement.work_area == left_screen
    assert_fully_visible(placement.top_left, size, left_screen)


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_logical_placement_remains_visible_for_synthetic_scaling(scale: float) -> None:
    def scaled(value: int) -> int:
        return round(value * scale)

    work_area = QRect(-scaled(1920), 0, scaled(1920), scaled(1040))
    pet = QRect(-scaled(250), scaled(760), scaled(192), scaled(208))
    size = QSize(scaled(380), scaled(560))

    placement = calculate_chat_panel_placement(
        pet,
        size,
        [work_area],
        gap=scaled(CHAT_PANEL_GAP),
    )

    assert placement.side == "left"
    assert_fully_visible(placement.top_left, size, work_area)


def test_compact_size_keeps_normal_width_and_adapts_to_short_work_area() -> None:
    assert compact_panel_size(QRect(0, 0, 1920, 1040)) == QSize(380, 560)
    assert compact_panel_size(QRect(-500, -200, 500, 400)) == QSize(380, 368)
    assert compact_panel_size(QRect(0, 0, 300, 240)) == QSize(268, 208)


def test_invalid_geometry_is_rejected() -> None:
    with pytest.raises(ValueError):
        calculate_chat_panel_placement(QRect(0, 0, 10, 10), QSize(10, 10), [])
    with pytest.raises(ValueError):
        compact_panel_size(QRect())
