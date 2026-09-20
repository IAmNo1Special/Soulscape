"""Per-monitor-v2 DPI awareness and WM_DPICHANGED handling."""

from __future__ import annotations

import sys
from ctypes import c_int, c_void_p
from typing import Any, NamedTuple

try:
    from ctypes import windll
except ImportError:
    windll = None

USER_DEFAULT_SCREEN_DPI = 96
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
WM_DPICHANGED = 0x02E0


class DpiChange(NamedTuple):
    dpi_x: int
    dpi_y: int
    scale: float
    suggested_rect: tuple[int, int, int, int]


def dpi_scale_factor(dpi: int) -> float:
    """Compute the UI scale factor for a DPI value.

    Args:
        dpi: Dots per inch reported by Windows.

    Returns:
        float: Scale factor relative to USER_DEFAULT_SCREEN_DPI (96).
    """
    return dpi / USER_DEFAULT_SCREEN_DPI


def parse_wm_dpichanged(
    wparam: int, suggested_rect: tuple[int, int, int, int]
) -> DpiChange:
    """Parse a WM_DPICHANGED message into DPI values and scale.

    Args:
        wparam: WPARAM carrying DPI_X in LOWORD and DPI_Y in HIWORD.
        suggested_rect: (left, top, right, bottom) rect suggested by Windows.

    Returns:
        DpiChange: Parsed DPI values, scale factor, and suggested rect.
    """
    dpi_x = wparam & 0xFFFF
    dpi_y = (wparam >> 16) & 0xFFFF
    return DpiChange(
        dpi_x=dpi_x,
        dpi_y=dpi_y,
        scale=dpi_scale_factor(dpi_x),
        suggested_rect=suggested_rect,
    )


def declare_per_monitor_v2_dpi_awareness(user32: Any = None) -> bool:
    """Declare per-monitor-v2 DPI awareness for the process.

    Must be called before the overlay window is created.

    Args:
        user32: Injectable user32 module (defaults to windll.user32 on win32).

    Returns:
        bool: True when the declaration succeeded.
    """
    if user32 is None:
        if sys.platform != "win32" or windll is None:
            return False
        user32 = windll.user32
    try:
        set_awareness = user32.SetProcessDpiAwarenessContext
        set_awareness.argtypes = [c_void_p]
        set_awareness.restype = c_int
        return bool(set_awareness(c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    except Exception:
        return False


def window_dpi(hwnd: int, user32: Any = None) -> int | None:
    """Return the current DPI for a window via GetDpiForWindow.

    Args:
        hwnd: Window handle to query.
        user32: Injectable user32 module (defaults to windll.user32 on win32).

    Returns:
        int | None: The window DPI, or None when unavailable.
    """
    if user32 is None:
        if sys.platform != "win32" or windll is None:
            return None
        user32 = windll.user32
    try:
        get_dpi = user32.GetDpiForWindow
        get_dpi.argtypes = [c_void_p]
        get_dpi.restype = c_int
        dpi = get_dpi(c_void_p(hwnd))
        return dpi if dpi > 0 else None
    except Exception:
        return None
