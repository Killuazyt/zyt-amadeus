from __future__ import annotations

import json

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPixmap

from amadeus_desktop.pet_assets import PetAssetService, validate_package
from amadeus_desktop.ui.pet_window import PetWindow


def make_window(tmp_path) -> PetWindow:
    asset = PetAssetService(tmp_path / "pets").load_builtin()
    return PetWindow(asset)


def make_low_resolution_asset(tmp_path):
    root = tmp_path / "low-resolution-pet"
    root.mkdir()
    document = {
        "schemaVersion": 1,
        "id": "low-resolution-test",
        "displayName": "Low Resolution Test",
        "description": "synthetic hot-replacement fixture",
        "kind": "generic",
        "author": "test",
        "source": "generated test fixture",
        "license": "CC0-1.0",
        "spritesheet": {
            "path": "spritesheet.webp",
            "frameWidth": 4,
            "frameHeight": 5,
            "logicalFrameWidth": 2,
            "logicalFrameHeight": 3,
            "columns": 1,
            "rows": 1,
        },
        "animations": {
            "idle": {
                "frames": [[0, 0]],
                "fps": 6,
                "loop": True,
                "fallback": "idle",
            }
        },
    }
    (root / "pet.amadeus.json").write_text(json.dumps(document), encoding="utf-8")
    sheet = QImage(4, 5, QImage.Format.Format_ARGB32)
    sheet.fill(Qt.GlobalColor.red)
    assert sheet.save(str(root / "spritesheet.webp"), "WEBP")
    return validate_package(root)


def send_mouse(
    window: PetWindow,
    event_type: QEvent.Type,
    global_point: QPoint,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
) -> None:
    local = global_point - window.pos()
    event = QMouseEvent(
        event_type,
        QPointF(local),
        QPointF(global_point),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    if event_type == QEvent.Type.MouseButtonPress:
        window.mousePressEvent(event)
    elif event_type == QEvent.Type.MouseMove:
        window.mouseMoveEvent(event)
    else:
        window.mouseReleaseEvent(event)


def test_window_flags_and_alpha_hit_region(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)

    flags = window.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.Tool
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert window.mask().contains(QPoint(window.width() // 2, round(window.height() * 0.65)))
    assert not window.mask().contains(QPoint(0, 0))


def test_builtin_frame_uses_high_resolution_source_and_logical_window(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)

    source = window._source_frame(window.current_frame).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    rendered_image = window._render_image(window.current_frame, 4.0).convertToFormat(
        QImage.Format.Format_RGBA8888
    )

    assert window.scale_percent == 100
    assert (window.width(), window.height()) == (192, 208)
    assert (source.width(), source.height()) == (768, 832)
    assert rendered_image.size() == source.size()
    assert rendered_image.devicePixelRatio() == 4.0
    assert rendered_image.bytesPerLine() == source.bytesPerLine()
    assert rendered_image.constBits().tobytes() == source.constBits().tobytes()


def test_rendered_frame_uses_physical_pixels_and_logical_hit_region(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_effective_device_pixel_ratio", lambda: 1.25)
    window._frame_cache.clear()

    pixmap, region = window._rendered_frame(window.current_frame)

    assert (pixmap.width(), pixmap.height()) == (240, 260)
    assert pixmap.devicePixelRatio() == 1.25
    assert (
        round(pixmap.deviceIndependentSize().width()),
        round(pixmap.deviceIndependentSize().height()),
    ) == (
        192,
        208,
    )
    assert region.boundingRect().right() < window.width()
    assert region.boundingRect().bottom() < window.height()
    assert any(key[2] == 1250 for key in window._frame_cache)


def test_dpr_change_event_invalidates_and_rebuilds_cache(
    qapp,
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    ratio = [1.0]
    monkeypatch.setattr(window, "_effective_device_pixel_ratio", lambda: ratio[0])
    window._frame_cache.clear()
    window._set_frame(window.current_frame)
    assert any(key[2] == 1000 for key in window._frame_cache)

    ratio[0] = 1.5
    event_type = getattr(QEvent.Type, "DevicePixelRatioChange", QEvent.Type.ScreenChangeInternal)
    qapp.sendEvent(window, QEvent(event_type))

    assert len(window._frame_cache) == 1
    assert next(iter(window._frame_cache))[2] == 1500
    assert (window._current_pixmap.width(), window._current_pixmap.height()) == (288, 312)
    assert window._current_pixmap.devicePixelRatio() == 1.5


def test_application_scale_and_dpr_render_matrix(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    cases = (
        (100, 1.0, (192, 208), (192, 208)),
        (100, 1.25, (192, 208), (240, 260)),
        (100, 1.5, (192, 208), (288, 312)),
        (100, 2.0, (192, 208), (384, 416)),
        (125, 1.25, (240, 260), (300, 325)),
        (150, 1.25, (288, 312), (360, 390)),
    )

    for scale_percent, dpr, logical_size, physical_size in cases:
        window.set_scale_percent(scale_percent)
        rendered = window._render_image(window.current_frame, dpr)
        pixmap = QPixmap.fromImage(rendered)

        assert (window.width(), window.height()) == logical_size
        assert (rendered.width(), rendered.height()) == physical_size
        assert rendered.devicePixelRatio() == dpr
        assert pixmap.devicePixelRatio() == dpr
        assert (
            round(pixmap.deviceIndependentSize().width()),
            round(pixmap.deviceIndependentSize().height()),
        ) == logical_size


def test_frame_cache_is_bounded_and_hot_replace_releases_high_resolution_sheet(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)

    for animation in window.asset.manifest.animations.values():
        for frame in animation.frames:
            window._rendered_frame(frame)

    assert len(window._frame_cache) == 16
    assert window._sheet.size().width() == 6144
    assert window._sheet.size().height() == 7488

    replacement = make_low_resolution_asset(tmp_path)
    window.replace_asset(replacement)

    assert window.asset.manifest.pet_id == "low-resolution-test"
    assert (window._sheet.width(), window._sheet.height()) == (4, 5)
    assert (window.width(), window.height()) == (2, 3)
    assert len(window._frame_cache) == 1
    pixmap, _region = next(iter(window._frame_cache.values()))
    assert (pixmap.width(), pixmap.height()) == (2, 3)


def test_show_starts_and_hide_pauses_animation(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    assert window.animation.is_running is False

    window.show()
    qtbot.waitUntil(window.isVisible)
    assert window.animation.is_running is True

    window.hide()
    assert window.animation.is_running is False


def test_scale_preserves_bottom_and_is_clamped(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    window.move(300, 400)
    old_bottom = window.geometry().bottom()

    window.set_scale_percent(150)

    assert window.scale_percent == 150
    assert window.geometry().bottom() == old_bottom
    window.set_scale_percent(999)
    assert window.scale_percent == 200


def test_click_and_drag_signals_are_exclusive_for_one_hundred_sequences(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    window.move(100, 100)
    clicks: list[bool] = []
    starts: list[bool] = []
    finishes: list[QPoint] = []
    directions: list[str] = []
    window.clicked.connect(lambda: clicks.append(True))
    window.drag_started.connect(lambda: starts.append(True))
    window.drag_finished.connect(finishes.append)
    window.drag_direction_changed.connect(directions.append)

    for _ in range(50):
        point = window.pos() + window.rect().center()
        send_mouse(
            window,
            QEvent.Type.MouseButtonPress,
            point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        send_mouse(
            window,
            QEvent.Type.MouseButtonRelease,
            point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )

    for index in range(50):
        start = window.pos() + window.rect().center()
        direction = 1 if index % 2 == 0 else -1
        end = start + QPoint(direction * (qapp.startDragDistance() + 20), 3)
        send_mouse(
            window,
            QEvent.Type.MouseButtonPress,
            start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        send_mouse(
            window,
            QEvent.Type.MouseMove,
            end,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        send_mouse(
            window,
            QEvent.Type.MouseButtonRelease,
            end,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )

    assert len(clicks) == 50
    assert len(starts) == 50
    assert len(finishes) == 50
    assert "move_left" in directions
    assert "move_right" in directions


def test_drag_keeps_original_mouse_anchor(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    window.move(200, 300)
    start_position = QPoint(window.pos())
    press = window.pos() + QPoint(30, 40)
    end = press + QPoint(75, -20)

    send_mouse(
        window,
        QEvent.Type.MouseButtonPress,
        press,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    send_mouse(
        window,
        QEvent.Type.MouseMove,
        end,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
    )

    assert window.pos() == start_position + QPoint(75, -20)


def test_click_only_emits_intent_and_does_not_choose_feedback(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    clicks: list[bool] = []
    window.clicked.connect(lambda: clicks.append(True))
    point = window.pos() + window.rect().center()

    send_mouse(
        window,
        QEvent.Type.MouseButtonPress,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    send_mouse(
        window,
        QEvent.Type.MouseButtonRelease,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )

    assert clicks == [True]
    assert window.animation.state == "idle"


def test_position_signal_follows_programmatic_moves(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    positions: list[QPoint] = []
    window.position_changed.connect(positions.append)
    window.show()
    qtbot.waitUntil(window.isVisible)

    window.move(240, 180)

    qtbot.waitUntil(lambda: bool(positions) and positions[-1] == QPoint(240, 180))
