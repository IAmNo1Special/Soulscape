"""Exclusive-fullscreen foreground detection for overlay parking."""

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

DESKTOP_WINDOW_CLASSES = frozenset({"Progman", "WorkerW"})

_CLASS_NAME_BUF = 256


def _foreground_window_class(fg: int, user32: Any) -> str:
    try:
        buf = ctypes.create_unicode_buffer(_CLASS_NAME_BUF)
        if user32.GetClassNameW(fg, buf, _CLASS_NAME_BUF) > 0:
            return buf.value
    except Exception:
        pass
    return ""


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


def rect_covers(outer: Rect, inner: Rect) -> bool:
    """Return True when rect `outer` fully covers rect `inner`.

    Args:
        outer: (left, top, right, bottom) of the candidate covering rect.
        inner: (left, top, right, bottom) of the rect to be covered.

    Returns:
        bool: True if outer covers inner on all four edges.
    """
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


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


def foreground_is_exclusive_fullscreen(
    own_hwnd: int | None = None, user32: Any = None
) -> bool:
    """Detect whether the foreground window is exclusive-fullscreen.

    Heuristic: the foreground window's rect covers the entire monitor rect.
    The overlay's own window never counts as fullscreen, and neither do
    the Windows desktop shell windows (Progman/WorkerW) -- clicking the
    desktop must not park the overlay.

    Args:
        own_hwnd: The overlay's own window handle, excluded from detection.
        user32: Injectable user32 module (defaults to windll.user32 on win32).

    Returns:
        bool: True when an exclusive-fullscreen foreground window is detected.
    """
    if user32 is None:
        if sys.platform != "win32" or windll is None:
            return False
        user32 = windll.user32
    try:
        fg = user32.GetForegroundWindow()
        if not fg or (own_hwnd is not None and fg == own_hwnd):
            return False
        if _foreground_window_class(fg, user32) in DESKTOP_WINDOW_CLASSES:
            return False
        fg_rect = _WinRect()
        if not user32.GetWindowRect(fg, ctypes.byref(fg_rect)):
            return False
        monitor_rect = monitor_rect_for_window(fg, user32=user32)
        if monitor_rect is None:
            return False
        return rect_covers(
            (fg_rect.left, fg_rect.top, fg_rect.right, fg_rect.bottom),
            monitor_rect,
        )
    except Exception:
        return False
