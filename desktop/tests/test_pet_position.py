from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize

from amadeus_desktop.pet_models import PetPosition
from amadeus_desktop.pet_position import (
    ScreenGeometry,
    capture_position,
    clamp_top_left,
    default_top_left,
    nearest_screen,
    restore_top_left,
)


def test_default_position_uses_bottom_right_margin() -> None:
    point = default_top_left(QSize(100, 120), QRect(0, 0, 1920, 1040))

    assert point == QPoint(1796, 896)


def test_capture_and_restore_preserves_relative_position_across_dpi_geometry() -> None:
    original = ScreenGeometry("secondary", QRect(-1600, 0, 1600, 900))
    position = capture_position(QPoint(-800, 390), QSize(100, 120), original)
    changed = ScreenGeometry("secondary", QRect(-1280, 0, 1280, 720))

    restored = restore_top_left(position, QSize(80, 96), [changed])

    assert restored.x() == -640
    assert restored.y() == 312


def test_missing_screen_falls_back_to_primary_and_stays_visible() -> None:
    primary = ScreenGeometry("primary", QRect(0, 0, 1920, 1040), primary=True)
    old = PetPosition("removed", 0.2, 0.8)

    restored = restore_top_left(old, QSize(200, 240), [primary])

    assert restored == QPoint(1696, 776)


def test_clamp_handles_negative_coordinate_screen() -> None:
    available = QRect(-1920, -1080, 1920, 1080)

    assert clamp_top_left(QPoint(-3000, -2000), QSize(200, 200), available) == QPoint(-1920, -1080)
    assert clamp_top_left(QPoint(100, 100), QSize(200, 200), available) == QPoint(-200, -200)


def test_nearest_screen_handles_gap_and_negative_layout() -> None:
    left = ScreenGeometry("left", QRect(-1920, 0, 1920, 1080))
    upper = ScreenGeometry("upper", QRect(0, -1440, 2560, 1440), primary=True)

    assert nearest_screen(QPoint(-100, 100), [left, upper]) == left
    assert nearest_screen(QPoint(100, -100), [left, upper]) == upper
