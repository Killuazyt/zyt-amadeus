"""Privacy-first visual source contracts and a capacity-one latest-frame slot."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class VisualSourceKind(StrEnum):
    SCREEN = "screen"
    WINDOW = "window"
    CAMERA = "camera"


class VisualSamplingEvent(StrEnum):
    USER_TEXT = "user_text"
    USER_VOICE = "user_voice"
    PROACTIVE_OPPORTUNITY = "proactive_opportunity"


@dataclass(frozen=True, slots=True)
class VisualFrame:
    """One sanitized PNG in volatile memory unless attached to a user turn."""

    source_kind: VisualSourceKind
    source_id: str
    source_name: str
    png_bytes: bytes
    captured_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("visual frames must be PNG")
        if self.captured_at.tzinfo is None:
            raise ValueError("visual frame timestamps must be timezone-aware")
        if self.sequence <= 0:
            raise ValueError("visual frame sequence must be positive")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png_bytes).hexdigest()


class LatestFrameSlot:
    """Thread-safe capacity-one slot; a new frame always overwrites the old one."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._frame: VisualFrame | None = None
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def put(self, frame: VisualFrame) -> int:
        if not isinstance(frame, VisualFrame):
            raise TypeError("latest frame must be a VisualFrame")
        with self._lock:
            self._generation += 1
            self._frame = frame
            return self._generation

    def snapshot(self) -> tuple[int, VisualFrame | None]:
        with self._lock:
            return self._generation, self._frame

    def clear(self) -> int:
        with self._lock:
            self._generation += 1
            self._frame = None
            return self._generation


class VisualSource(Protocol):
    kind: VisualSourceKind

    @property
    def active(self) -> bool: ...

    @property
    def source_name(self) -> str: ...

    def start(self, source_id: str = "") -> bool: ...

    def stop(self) -> None: ...


def visual_timestamp() -> datetime:
    return datetime.now(UTC)
