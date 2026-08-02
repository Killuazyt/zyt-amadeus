"""Prime Windows platform metadata before third-party PyInstaller runtime hooks."""

from __future__ import annotations

import platform
import sys

if (
    sys.platform == "win32"
    and hasattr(sys, "getwindowsversion")
    and getattr(platform, "_uname_cache", None) is None
):
    original_probe = getattr(platform, "_syscmd_ver", None)
    if callable(original_probe):
        windows_version = sys.getwindowsversion()
        native_version = windows_version[:3]
        version_text = ".".join(str(component) for component in native_version)

        def native_probe(*_arguments: object, **_keywords: object) -> tuple[str, str, str]:
            return "Microsoft Windows", "", version_text

        platform._syscmd_ver = native_probe
        try:
            platform.uname()
        finally:
            platform._syscmd_ver = original_probe
