# window_manager.py
"""Platform-agnostic window management abstraction."""

import sys
from abc import ABC, abstractmethod
from ctypes import c_void_p, create_unicode_buffer, windll

from logger import log


class WindowManager(ABC):
    """Abstract base class for platform-specific window management."""

    @abstractmethod
    def apply_transparency(self, window, window_id: int) -> bool:
        """
        Apply transparency settings to a window.

        Args:
            window: Pyglet window instance
            window_id: Unique identifier for the window

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def set_always_on_top(self, window, window_id: int, enabled: bool = True) -> bool:
        """
        Set whether a window stays on top of other windows.

        Args:
            window: Pyglet window instance
            window_id: Unique identifier for the window
            enabled: True to enable always-on-top

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def hide_from_taskbar(self, window, window_id: int) -> bool:
        """
        Hide the window from the taskbar.

        Args:
            window: Pyglet window instance
            window_id: Unique identifier for the window

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def set_opacity(self, window, window_id: int, opacity: int) -> bool:
        """
        Set window opacity.

        Args:
            window: Pyglet window instance
            window_id: Unique identifier for the window
            opacity: Opacity percentage (0-100)

        Returns:
            True if successful, False otherwise
        """
        pass

    def setup_window(self, window, window_id: int) -> bool:
        """
        Apply all standard window settings (transparency, topmost, hide from taskbar).

        Args:
            window: Pyglet window instance
            window_id: Unique identifier for the window

        Returns:
            True if all operations successful
        """
        success = True
        success &= self.apply_transparency(window, window_id)
        success &= self.set_always_on_top(window, window_id, True)
        success &= self.hide_from_taskbar(window, window_id)
        return success


class WindowsWindowManager(WindowManager):
    """Windows-specific window manager using Win32 API."""

    # Win32 constants
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080
    LWA_COLORKEY = 0x0001
    LWA_ALPHA = 0x0002
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010

    def _get_hwnd(self, window, window_id: int):
        """Get the Win32 window handle for a Pyglet window."""
        # Try to get internal handle directly from Pyglet window first
        if hasattr(window, "_hwnd"):
            return window._hwnd

        # Fallback to legacy method
        window_title = f"SoulWindow_{window_id}"
        window.set_caption(window_title)
        hwnd = windll.user32.FindWindowW(None, create_unicode_buffer(window_title))
        if not hwnd:
            log.warning(
                f"Could not find window by caption. "
                f"Falling back to GetForegroundWindow for soul {window_id}."
            )
            hwnd = windll.user32.GetForegroundWindow()
        return hwnd

    def apply_transparency(self, window, window_id: int) -> bool:
        """Apply layered window with color-key transparency."""
        try:
            hwnd = self._get_hwnd(window, window_id)
            if not hwnd:
                return False

            style = windll.user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            new_style = style | self.WS_EX_LAYERED
            new_style &= ~self.WS_EX_TRANSPARENT  # Keep window clickable
            windll.user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, new_style)

            # Color key: black (0x000000) becomes transparent
            windll.user32.SetLayeredWindowAttributes(
                hwnd, 0x000000, 0, self.LWA_COLORKEY
            )
            log.debug(f"Transparency applied for soul {window_id}.")
            return True

        except Exception as e:
            log.error(f"Error applying transparency for soul {window_id}: {e}")
            return False

    def set_always_on_top(self, window, window_id: int, enabled: bool = True) -> bool:
        """Set window as always-on-top using SetWindowPos."""
        try:
            hwnd = self._get_hwnd(window, window_id)
            if not hwnd:
                return False

            z_order = c_void_p(-1 if enabled else -2)  # HWND_TOPMOST or HWND_NOTOPMOST

            # For NOTOPMOST, need to use different approach
            SWP_FRAMECHANGED = 0x0020
            flags = (
                self.SWP_NOMOVE
                | self.SWP_NOSIZE
                | self.SWP_NOACTIVATE
                | SWP_FRAMECHANGED
            )

            result = windll.user32.SetWindowPos(
                hwnd,
                z_order,
                0,
                0,
                0,
                0,
                flags,
            )

            log.info(
                f"Always-on-top {'enabled' if enabled else 'disabled'} for soul {window_id}. "
                f"SetWindowPos result: {result}"
            )
            return bool(result)

        except Exception as e:
            log.error(f"Error setting always-on-top for soul {window_id}: {e}")
            return False

    def hide_from_taskbar(self, window, window_id: int) -> bool:
        """Hide window from taskbar using WS_EX_TOOLWINDOW."""
        try:
            hwnd = self._get_hwnd(window, window_id)
            if not hwnd:
                return False

            style = windll.user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            new_style = (style | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
            windll.user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, new_style)
            log.debug(f"Hidden from taskbar for soul {window_id}.")
            return True

        except Exception as e:
            log.error(f"Error hiding from taskbar for soul {window_id}: {e}")
            return False

    def set_opacity(self, window, window_id: int, opacity: int) -> bool:
        """Set window opacity using LWA_ALPHA combined with color key."""
        try:
            hwnd = self._get_hwnd(window, window_id)
            if not hwnd:
                return False

            # Clamp opacity to valid range
            opacity = max(0, min(100, opacity))
            alpha = int((opacity / 100) * 255)

            # Combine color key (for background) with alpha (for overall opacity)
            # Using LWA_COLORKEY | LWA_ALPHA allows both
            windll.user32.SetLayeredWindowAttributes(
                hwnd, 0x000000, alpha, self.LWA_COLORKEY | self.LWA_ALPHA
            )
            log.info(f"Opacity set to {opacity}% for soul {window_id}.")
            return True

        except Exception as e:
            log.error(f"Error setting opacity for soul {window_id}: {e}")
            return False


class StubWindowManager(WindowManager):
    """Stub implementation for unsupported platforms."""

    def apply_transparency(self, window, window_id: int) -> bool:
        log.warning(f"Transparency not supported on this platform (soul {window_id}).")
        return False

    def set_always_on_top(self, window, window_id: int, enabled: bool = True) -> bool:
        log.warning(f"Always-on-top not supported on this platform (soul {window_id}).")
        return False

    def hide_from_taskbar(self, window, window_id: int) -> bool:
        log.warning(
            f"Hide from taskbar not supported on this platform (soul {window_id})."
        )
        return False

    def set_opacity(self, window, window_id: int, opacity: int) -> bool:
        log.warning(f"Opacity not supported on this platform (soul {window_id}).")
        return False


def get_window_manager() -> WindowManager:
    """
    Factory function that returns the appropriate WindowManager for the current platform.

    Returns:
        WindowManager instance for the current platform
    """
    if sys.platform == "win32":
        return WindowsWindowManager()
    # Future: macOS and Linux implementations
    # elif sys.platform == "darwin":
    #     return MacOSWindowManager()
    # elif sys.platform.startswith("linux"):
    #     return LinuxWindowManager()
    else:
        log.warning(
            f"Platform '{sys.platform}' not fully supported. Using stub manager."
        )
        return StubWindowManager()
