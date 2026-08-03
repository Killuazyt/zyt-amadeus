"""Read-only Windows presence probes used to suppress intrusive greetings."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    idle_seconds: float
    session_locked: bool
    fullscreen: bool


class PresenceProbe:
    def snapshot(self) -> PresenceSnapshot:
        raise NotImplementedError


class WindowsPresenceProbe(PresenceProbe):
    """Inspect input/session/window geometry without reading titles or pixels."""

    def snapshot(self) -> PresenceSnapshot:
        if os.name != "nt":
            return PresenceSnapshot(0.0, False, False)
        return PresenceSnapshot(
            idle_seconds=_idle_seconds(),
            session_locked=_session_locked(),
            fullscreen=_foreground_is_fullscreen(),
        )


class _LastInputInfo(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


class _MonitorInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    )


def _idle_seconds() -> float:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    kernel32.GetTickCount64.argtypes = []
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed = _elapsed_tick_milliseconds(int(kernel32.GetTickCount64()), int(info.dwTime))
    return max(0.0, elapsed / 1000.0)


def _elapsed_tick_milliseconds(current_tick_64: int, last_input_tick_32: int) -> int:
    """Compare LASTINPUTINFO's DWORD timestamp in its native wrapping domain."""

    current_tick_32 = int(current_tick_64) & 0xFFFFFFFF
    return (current_tick_32 - int(last_input_tick_32)) & 0xFFFFFFFF


def _session_locked() -> bool:
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desktop:
        return True
    user32.CloseDesktop(desktop)
    return False


def _foreground_is_fullscreen() -> bool:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetDesktopWindow.argtypes = []
    user32.GetDesktopWindow.restype = wintypes.HWND
    user32.GetShellWindow.argtypes = []
    user32.GetShellWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    foreground = user32.GetForegroundWindow()
    if not foreground or foreground in {user32.GetDesktopWindow(), user32.GetShellWindow()}:
        return False
    rectangle = wintypes.RECT()
    if not user32.GetWindowRect(foreground, ctypes.byref(rectangle)):
        return False
    monitor = user32.MonitorFromWindow(foreground, 2)  # MONITOR_DEFAULTTONEAREST
    if not monitor:
        return False
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    tolerance = 2
    return (
        rectangle.left <= info.rcMonitor.left + tolerance
        and rectangle.top <= info.rcMonitor.top + tolerance
        and rectangle.right >= info.rcMonitor.right - tolerance
        and rectangle.bottom >= info.rcMonitor.bottom - tolerance
    )
