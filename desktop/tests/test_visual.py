from __future__ import annotations

import base64
import io
from datetime import UTC, datetime

import pytest
from PIL import Image
from PySide6.QtGui import QImage

from amadeus_desktop.chat_models import ImagePart, ProviderRoute, TextPart
from amadeus_desktop.proactive import ProactiveTrigger, build_proactive_visual_request
from amadeus_desktop.visual import LatestFrameSlot, VisualFrame, VisualSourceKind
from amadeus_desktop.visual_runtime import (
    MAX_VISUAL_EDGE,
    VisualSourceManager,
    _encode_qimage_png,
)


def _png(color: int = 80) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), (color, 40, 20)).save(output, format="PNG")
    return output.getvalue()


def _frame(sequence: int, *, color: int = 80) -> VisualFrame:
    return VisualFrame(
        VisualSourceKind.SCREEN,
        "display-1",
        "private source name",
        _png(color),
        datetime(2026, 8, 10, 9, tzinfo=UTC),
        sequence,
    )


def test_latest_frame_slot_overwrites_without_queue_and_clear_invalidates() -> None:
    slot = LatestFrameSlot()
    first = _frame(1)
    second = _frame(2, color=120)

    first_generation = slot.put(first)
    second_generation = slot.put(second)

    generation, latest = slot.snapshot()
    assert first_generation == 1
    assert second_generation == generation == 2
    assert latest is second
    assert slot.clear() == 3
    assert slot.snapshot() == (3, None)


def test_visual_frame_requires_sanitized_png_and_aware_time() -> None:
    try:
        VisualFrame(
            VisualSourceKind.CAMERA,
            "camera",
            "camera",
            b"not-png",
            datetime.now(UTC),
            1,
        )
    except ValueError as exc:
        assert "PNG" in str(exc)
    else:
        raise AssertionError("non-PNG frame was accepted")


def test_qimage_encoder_caps_longest_edge_and_removes_alpha() -> None:
    image = QImage(3_000, 30, QImage.Format.Format_RGBA8888)
    image.fill(0x80FF0000)

    payload = _encode_qimage_png(image)

    with Image.open(io.BytesIO(payload)) as encoded:
        encoded.load()
        assert max(encoded.size) == MAX_VISUAL_EDGE
        assert encoded.mode == "RGB"


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_qimage_encoder_keeps_dpi_physical_pixels_without_double_scaling(scale: float) -> None:
    physical_size = (round(320 * scale), round(180 * scale))
    image = QImage(*physical_size, QImage.Format.Format_RGB888)
    image.setDevicePixelRatio(scale)
    image.fill(0x224466)

    payload = _encode_qimage_png(image)

    with Image.open(io.BytesIO(payload)) as encoded:
        encoded.load()
        assert encoded.size == physical_size


def test_visual_source_manager_switches_exclusively_and_clears_old_frame(qtbot) -> None:
    class _Source:
        def __init__(self, name: str) -> None:
            self.source_name = name
            self.starts = 0
            self.stops = 0

        def start(self, _source_id: str = "") -> bool:
            self.starts += 1
            return True

        def stop(self) -> None:
            self.stops += 1

    manager = VisualSourceManager()
    screen = _Source("screen")
    window = _Source("window")
    camera = _Source("camera")
    manager.screen = screen  # type: ignore[assignment]
    manager.window = window  # type: ignore[assignment]
    manager.camera = camera  # type: ignore[assignment]

    assert manager.start(VisualSourceKind.SCREEN)
    manager._on_frame(_frame(1))
    assert manager.latest() is not None
    assert manager.start(VisualSourceKind.CAMERA)

    assert manager.active_kind is VisualSourceKind.CAMERA
    assert manager.latest() is None
    assert screen.starts == 1
    assert camera.starts == 1
    assert screen.stops >= 2
    assert window.stops >= 2


def test_proactive_visual_request_contains_one_volatile_frame_and_no_history() -> None:
    frame = _frame(1)
    request = build_proactive_visual_request(
        datetime(2026, 8, 10, 9, tzinfo=UTC),
        ProactiveTrigger.IDLE,
        frame,
    )

    assert request.provider_route is ProviderRoute.MULTIMODAL
    assert request.attachments == ()
    assert request.turn_id.startswith("proactive-visual:")
    assert len(request.messages) == 3
    content = request.messages[-1].content
    assert isinstance(content, tuple)
    assert isinstance(content[0], TextPart)
    assert isinstance(content[1], ImagePart)
    assert content[1].attachment_id == f"volatile:{frame.sha256}"
    assert base64.b64decode(content[1].data_url.partition(",")[2]) == frame.png_bytes
    assert frame.source_name not in content[0].text
