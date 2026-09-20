"""Platform-agnostic window management abstraction."""

from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from time import sleep

try:
    from ctypes import c_void_p, create_unicode_buffer, windll
except ImportError:
    c_void_p = None
    create_unicode_buffer = None
    windll = None

import pyglet

from .dpi import parse_wm_dpichanged, window_dpi
from .fullscreen import monitor_rect_for_window
from .logger import log
from .window import SoulscapeWindow


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos() -> tuple[int, int] | None:
    """Return the cursor position in screen coords (top-left origin).

    Returns:
        tuple[int, int] | None: (x, y) from GetCursorPos, or None when
        unavailable (non-Windows or Win32 call failed).
    """
    if sys.platform != "win32" or windll is None:
        return None
    try:
        pt = _Point()
        if windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
    except Exception:
        pass
    return None


class WindowManager(ABC):
    """Abstract base class for platform-specific window management.

    Attributes:
        window: The Pyglet window instance.
        hwnd: The platform-specific window handle (if available).
        batch: The Pyglet graphics batch for drawing.
    """

    def __init__(self) -> None:
        """Initializes the WindowManager and sets up the window."""
        self.window = SoulscapeWindow()
        self.hwnd: int | None = self.get_hwnd()
        self.batch = pyglet.graphics.Batch()
        self.setup_window()

    def setup_window(self) -> bool:
        """Apply all standard window settings (transparency, topmost, hide from taskbar).

        Returns:
            bool: True if all settings were applied successfully, False otherwise.
        """
        success = True
        try:
            # sleep to ensure window is created
            sleep(0.5)
            success &= self._remove_window_border()
            success &= self._hide_from_taskbar()
            success &= self._apply_transparency()
            success &= self.set_always_on_top(True)
        except Exception as e:
            log.error(f"Error setting up window: {e}")
            success = False

        return success

    @abstractmethod
    def get_hwnd(self) -> int | None:
        """Get the platform-specific window handle.

        Returns:
            int | None: The window handle if available, None otherwise.
        """
        pass

    @abstractmethod
    def set_always_on_top(self, enabled: bool = True) -> bool:
        """Set whether a window stays on top of other windows.

        Args:
            enabled: True to enable always-on-top, False to disable.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def set_opacity(self, opacity: float) -> bool:
        """Set window opacity.

        Args:
            opacity: Opacity value between 0.0 (transparent) and 100.0 (opaque).

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def _apply_transparency(self) -> bool:
        """Apply transparency settings to a window.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def _remove_window_border(self) -> bool:
        """Remove window border and caption.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def _hide_from_taskbar(self) -> bool:
        """Hide the window from the taskbar.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

    def show_window(self) -> bool:
        """Show the overlay window without stealing focus.

        The never-steal-focus policy requires activation-free showing;
        platform managers override this with the native no-activate
        mechanism (SW_SHOWNA on Windows).

        Returns:
            bool: True if the window was shown.
        """
        self.window.set_visible(True)
        return True


class StubWindowManager(WindowManager):
    """Stub implementation for unsupported platforms."""

    def __init__(self) -> None:
        super().__init__()

    def get_hwnd(self) -> int | None:
        """Get the window handle (stub)."""
        return None

    def set_always_on_top(self, enabled: bool = True) -> bool:
        """Set always on top (stub)."""
        log.warning("Always-on-top not supported on this platform.")
        return False

    def set_opacity(self, opacity: float) -> bool:
        """Set opacity (stub)."""
        log.warning("Opacity setting not supported on this platform.")
        return False

    def _apply_transparency(self) -> bool:
        """Apply transparency (stub)."""
        log.warning("Transparency not supported on this platform.")
        return False

    def _remove_window_border(self) -> bool:
        """Remove window border (stub)."""
        log.warning("Window border removal not supported on this platform.")
        return False

    def _hide_from_taskbar(self) -> bool:
        """Hide from taskbar (stub)."""
        log.warning("Hide from taskbar not supported on this platform.")
        return False


class WindowsWindowManager(WindowManager):
    """Windows-specific implementation of window management."""

    # Win32 constants
    GWL_STYLE = -16
    GWL_EXSTYLE = -20

    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2

    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000

    WS_EX_LAYERED = 0x00097850
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_TOPMOST = 0x00000008
    WS_EX_NOACTIVATE = 0x08000000

    SW_SHOWNA = 8

    LWA_COLORKEY = 0x0001
    LWA_ALPHA = 0x0002

    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    SWP_NOACTIVATE = 0x0010

    def __init__(self) -> None:
        """Initializes the WindowsWindowManager."""
        super().__init__()
        self._current_opacity = 255  # Default to full opacity
        self._click_through = True
        self._dpi: int | None = None

    def get_hwnd(self) -> int | None:
        """Get the Win32 window handle for a Pyglet window.

        Returns:
            int | None: The window handle.
        """
        # Try to get internal handle directly from Pyglet window first
        log.debug("Getting window handle for Soulscape.")
        if hasattr(self.window, "_hwnd"):
            log.debug("Found window handle for Soulscape.")
            return self.window._hwnd

        log.warning(
            "Could not find window handle for Soulscape. "
            "Falling back to GetForegroundWindow."
        )
        window_title = "Soulscape"
        self.window.set_caption(window_title)

        if windll:
            hwnd = windll.user32.FindWindowW(None, create_unicode_buffer(window_title))
            if not hwnd:
                log.warning(
                    f"Could not find {window_title} window handle by caption. "
                    "Falling back to GetForegroundWindow."
                )
                hwnd = windll.user32.GetForegroundWindow()
            return hwnd
        return None

    def on_draw(self) -> None:
        """Handle the draw event."""
        self.window.clear()
        self.batch.draw()

    def set_always_on_top(self, enabled: bool = True) -> bool:
        """Set window as always-on-top using SetWindowPos.

        Args:
            enabled: True to enable always-on-top, False to disable.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            if not self.hwnd:
                log.warning("Could not find window handle for Soulscape.")
                return False

            z_order = c_void_p(self.HWND_TOPMOST if enabled else self.HWND_NOTOPMOST)

            flags = (
                self.SWP_NOMOVE
                | self.SWP_NOSIZE
                | self.SWP_NOACTIVATE
                | self.SWP_FRAMECHANGED
            )

            result = ctypes.windll.user32.SetWindowPos(
                self.hwnd,
                z_order,
                0,
                0,
                0,
                0,
                flags,
            )
            return bool(result)

        except Exception as e:
            log.error(f"Error setting always-on-top: {e}")
            return False

    def show_window(self) -> bool:
        """Show the overlay via SW_SHOWNA so it never steals focus.

        Returns:
            bool: True if the window was shown.
        """
        try:
            if self.hwnd and windll:
                return bool(windll.user32.ShowWindow(self.hwnd, self.SW_SHOWNA))
            self.window.set_visible(True)
            return False
        except Exception as e:
            log.error(f"Error showing window without activation: {e}")
            return False

    def set_click_through(self, enabled: bool) -> bool:
        """Toggle WS_EX_TRANSPARENT on the overlay window.

        Args:
            enabled: True makes clicks pass through the window; False makes
                the window clickable again.

        Returns:
            bool: True if the style was applied.
        """
        try:
            if not self.hwnd or windll is None:
                return False

            style = windll.user32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
            if enabled:
                new_style = style | self.WS_EX_TRANSPARENT
            else:
                new_style = style & ~self.WS_EX_TRANSPARENT
            windll.user32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE, new_style)
            return True

        except Exception as e:
            log.error(f"Error toggling click-through: {e}")
            return False

    def update_click_through(self, soul_under_cursor: object | None) -> bool:
        """Apply click-through unless the cursor is over a Soul.

        Idempotent: the Win32 style is only touched when the desired state
        changes, so the 12Hz cursor poll costs nothing when idle.

        Args:
            soul_under_cursor: The Soul under the cursor, or None.

        Returns:
            bool: The effective click-through state.
        """
        want_click_through = soul_under_cursor is None
        if want_click_through != self._click_through:
            if self.set_click_through(want_click_through):
                self._click_through = want_click_through
        return self._click_through

    def current_dpi(self) -> int | None:
        """Return the current DPI of the overlay window.

        Returns:
            int | None: The window DPI, or None when unavailable.
        """
        if not self.hwnd:
            return None
        return window_dpi(self.hwnd)

    def check_dpi_changed(self) -> bool:
        """Poll for DPI changes and re-assert the window rect when changed.

        Returns:
            bool: True when a DPI change was detected and applied.
        """
        dpi = self.current_dpi()
        if dpi is None:
            return False
        if self._dpi is None:
            self._dpi = dpi
            return False
        if dpi != self._dpi:
            self._dpi = dpi
            self.apply_monitor_rect()
            return True
        return False

    def apply_monitor_rect(self) -> bool:
        """Re-assert the fullscreen monitor rect without activating.

        Repositioning in one SetWindowPos avoids ghosting artifacts that
        appear when the window is resized in multiple steps.

        Returns:
            bool: True if the rect was applied.
        """
        try:
            if not self.hwnd or windll is None:
                return False
            rect = monitor_rect_for_window(self.hwnd)
            if rect is None:
                return False
            left, top, right, bottom = rect
            flags = self.SWP_NOZORDER | self.SWP_NOACTIVATE | self.SWP_FRAMECHANGED
            return bool(
                windll.user32.SetWindowPos(
                    self.hwnd,
                    None,
                    left,
                    top,
                    right - left,
                    bottom - top,
                    flags,
                )
            )
        except Exception as e:
            log.error(f"Error applying monitor rect: {e}")
            return False

    def on_wm_dpichanged(
        self,
        wparam: int,
        suggested_rect: tuple[int, int, int, int],
    ) -> bool:
        """Apply a WM_DPICHANGED message using Windows' suggested rect.

        Args:
            wparam: WPARAM carrying the new DPI values.
            suggested_rect: (left, top, right, bottom) suggested by Windows.

        Returns:
            bool: True if the suggested rect was applied.
        """
        try:
            if not self.hwnd or windll is None:
                return False
            change = parse_wm_dpichanged(wparam, suggested_rect)
            self._dpi = change.dpi_x
            left, top, right, bottom = change.suggested_rect
            flags = self.SWP_NOZORDER | self.SWP_NOACTIVATE | self.SWP_FRAMECHANGED
            return bool(
                windll.user32.SetWindowPos(
                    self.hwnd,
                    None,
                    left,
                    top,
                    right - left,
                    bottom - top,
                    flags,
                )
            )
        except Exception as e:
            log.error(f"Error handling WM_DPICHANGED: {e}")
            return False

    def set_opacity(self, opacity: float) -> bool:
        """Set window opacity.

        Args:
            opacity: Opacity value between 0.0 (transparent) and 100.0 (opaque).

        Returns:
            bool: True if successful.
        """
        try:
            if not self.hwnd:
                return False

            # Clamp and convert to 0-255
            alpha = max(0, min(255, int((opacity / 100.0) * 255)))
            self._current_opacity = alpha

            # We need to re-apply transparency to update attributes
            return self._apply_transparency()

        except Exception as e:
            log.error(f"Error setting opacity: {e}")
            return False

    def _apply_transparency(self) -> bool:
        """Apply layered window with color-key transparency and alpha.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            if not self.hwnd:
                return False

            if not windll:
                return False

            # Ensure _current_opacity is set
            if not hasattr(self, "_current_opacity"):
                self._current_opacity = 255

            style = windll.user32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
            new_style = (
                style
                | self.WS_EX_LAYERED
                | self.WS_EX_NOACTIVATE
                | self.WS_EX_TRANSPARENT
            )
            windll.user32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE, new_style)

            # Flags: Always LWA_COLORKEY. Add LWA_ALPHA if we want opacity.
            flags = self.LWA_COLORKEY
            flags |= self.LWA_ALPHA

            windll.user32.SetLayeredWindowAttributes(
                self.hwnd, 0x000000, self._current_opacity, flags
            )
            log.debug(f"Transparency applied (Alpha: {self._current_opacity}).")
            return True

        except Exception as e:
            log.error(f"Error applying transparency for Soulscape: {e}")
            return False

    def _remove_window_border(self) -> bool:
        """Remove window border using WS_EX_LAYERED.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            if not self.hwnd:
                return False

            if not windll:
                return False

            style = ctypes.windll.user32.GetWindowLongW(self.hwnd, self.GWL_STYLE)
            ctypes.windll.user32.SetWindowLongW(
                self.hwnd,
                self.GWL_STYLE,
                style & ~self.WS_CAPTION & ~self.WS_THICKFRAME,
            )
            log.debug("Window border removed.")
            return True
        except Exception as e:
            log.error(f"Error removing window border: {e}")
            return False

    def _hide_from_taskbar(self) -> bool:
        """Hide window from taskbar using WS_EX_TOOLWINDOW.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            if not self.hwnd:
                return False

            if not getattr(ctypes, "windll", None):
                return False

            style = ctypes.windll.user32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
            new_style = (style | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE, new_style)
            log.debug("Hidden from taskbar.")
            return True

        except Exception as e:
            log.error(f"Error hiding from taskbar: {e}")
            return False


def get_window_manager() -> WindowManager:
    """Factory function that returns the appropriate WindowManager for the current platform.

    Returns:
        WindowManager: Instance for the current platform.
    """
    if sys.platform == "win32":
        return WindowsWindowManager()
    # TODO: macOS and Linux implementations
    # elif sys.platform == "darwin":
    #     return MacOSWindowManager()
    # elif sys.platform.startswith("linux"):
    #     return LinuxWindowManager()
    else:
        log.warning(
            f"Platform '{sys.platform}' not fully supported. Using stub manager."
        )
        return StubWindowManager()


if __name__ == "__main__":
    raise NotImplementedError("This module is not intended to be run directly.")
