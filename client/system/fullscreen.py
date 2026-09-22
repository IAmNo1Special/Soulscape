"""Monitor geometry helpers for the overlay window."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

try:
    from ctypes import windll
except ImportError:
    windll = None

Rect = tuple[int, int, int, int]

MONITOR_DEFAULTTONEAREST = 2


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _WinRect),
        ("rcWork", _WinRect),
        ("dwFlags", ctypes.c_ulong),
    ]


def monitor_rect_for_window(hwnd: int, user32: Any = None) -> Rect | None:
    """Return the full monitor rect for the monitor containing `hwnd`.

    Args:
        hwnd: Window handle to locate the monitor for.
        user32: Injectable user32 module (defaults to windll.user32 on win32).

    Returns:
        (left, top, right, bottom) of the monitor, or None when unavailable.
    """
    if user32 is None:
        if sys.platform != "win32" or windll is None:
            return None
        user32 = windll.user32
    try:
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rc = info.rcMonitor
        return (rc.left, rc.top, rc.right, rc.bottom)
    except Exception:
        return None
