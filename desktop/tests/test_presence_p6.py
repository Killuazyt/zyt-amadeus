from __future__ import annotations

from amadeus_desktop.presence import _elapsed_tick_milliseconds


def test_last_input_tick_uses_dword_wrap_domain_after_long_windows_uptime() -> None:
    wrap = 1 << 32

    assert _elapsed_tick_milliseconds(wrap + 1_250, 750) == 500
    assert _elapsed_tick_milliseconds(wrap + 500, wrap - 500) == 1_000
