"""Pure data models for desktop-pet resources and placement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FrameCoordinate:
    column: int
    row: int


@dataclass(frozen=True, slots=True)
class AnimationSpec:
    name: str
    frames: tuple[FrameCoordinate, ...]
    fps: float
    loop: bool
    fallback: str


@dataclass(frozen=True, slots=True)
class SpriteSheetSpec:
    path: str
    frame_width: int
    frame_height: int
    columns: int
    rows: int
    default_scale_percent: int
    logical_frame_width: int
    logical_frame_height: int
    alpha_threshold: int = 8
    hit_padding: int = 2


@dataclass(frozen=True, slots=True)
class PetManifest:
    schema_version: int
    pet_id: str
    display_name: str
    description: str
    kind: str
    author: str
    source: str
    license_name: str
    spritesheet: SpriteSheetSpec
    animations: dict[str, AnimationSpec]
    compatibility_profile: str | None = None

    def animation(self, name: str) -> AnimationSpec:
        """Resolve an action through its declared fallback and finally idle."""

        visited: set[str] = set()
        current = name
        while current not in visited:
            visited.add(current)
            animation = self.animations.get(current)
            if animation is not None:
                return animation
            current = "idle"
        return self.animations["idle"]


@dataclass(frozen=True, slots=True)
class PetPosition:
    screen_id: str
    x_ratio: float
    y_ratio: float

    def to_document(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id,
            "x_ratio": self.x_ratio,
            "y_ratio": self.y_ratio,
        }

    @classmethod
    def from_document(cls, value: Any) -> PetPosition | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            return None
        screen_id = value.get("screen_id")
        x_ratio = value.get("x_ratio")
        y_ratio = value.get("y_ratio")
        if not isinstance(screen_id, str) or not screen_id:
            return None
        if isinstance(x_ratio, bool) or not isinstance(x_ratio, (int, float)):
            return None
        if isinstance(y_ratio, bool) or not isinstance(y_ratio, (int, float)):
            return None
        if not 0.0 <= float(x_ratio) <= 1.0 or not 0.0 <= float(y_ratio) <= 1.0:
            return None
        return cls(screen_id, float(x_ratio), float(y_ratio))
