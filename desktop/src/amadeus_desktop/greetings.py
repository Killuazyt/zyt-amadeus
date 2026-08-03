"""Strict local greeting catalogs with a public-safe bundled fallback."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from amadeus_desktop.proactive import ProactiveTrigger

MAX_GREETING_FILE_BYTES = 32 * 1024
MAX_GREETINGS_PER_TRIGGER = 20
MAX_GREETING_CHARS = 120

_BUNDLED = {
    ProactiveTrigger.STARTUP: (
        "我在。先把今天过得稳一点吧。",
        "早，别急着把所有事情一次做完。",
        "已经启动了。需要聊聊时点我就好。",
    ),
    ProactiveTrigger.IDLE: (
        "休息一下也不算偷懒，真的。",
        "坐得有点久了吧，记得活动一下。",
        "还在忙？至少先喝口水。",
    ),
}


class GreetingCatalogError(ValueError):
    """Raised without including private greeting text or a source path."""


@dataclass(frozen=True, slots=True)
class GreetingCatalog:
    startup: tuple[str, ...]
    idle: tuple[str, ...]

    def choose(self, trigger: ProactiveTrigger, seed: str) -> str:
        values = self.startup if trigger is ProactiveTrigger.STARTUP else self.idle
        digest = hashlib.sha256(f"{trigger.value}\0{seed}".encode()).digest()
        return values[int.from_bytes(digest[:8], "big") % len(values)]


def load_greeting_catalog(path: Path | None) -> GreetingCatalog:
    if path is None or not path.exists():
        return bundled_greeting_catalog()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GreetingCatalogError("local greeting catalog could not be read") from exc
    if len(payload) > MAX_GREETING_FILE_BYTES:
        raise GreetingCatalogError("local greeting catalog is too large")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GreetingCatalogError("local greeting catalog is invalid") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "startup", "idle"}:
        raise GreetingCatalogError("local greeting catalog schema is invalid")
    if document["schema_version"] != 1:
        raise GreetingCatalogError("local greeting catalog version is unsupported")
    return GreetingCatalog(
        startup=_validated_greetings(document["startup"]),
        idle=_validated_greetings(document["idle"]),
    )


def bundled_greeting_catalog() -> GreetingCatalog:
    return GreetingCatalog(_BUNDLED[ProactiveTrigger.STARTUP], _BUNDLED[ProactiveTrigger.IDLE])


def _validated_greetings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_GREETINGS_PER_TRIGGER:
        raise GreetingCatalogError("local greeting catalog entries are invalid")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GreetingCatalogError("local greeting catalog entries are invalid")
        normalized = item.strip()
        if (
            not normalized
            or len(normalized) > MAX_GREETING_CHARS
            or any(ord(character) < 32 and character not in "\t" for character in normalized)
        ):
            raise GreetingCatalogError("local greeting catalog entries are invalid")
        values.append(normalized)
    return tuple(values)
