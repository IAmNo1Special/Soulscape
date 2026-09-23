"""Unit tests for Win32 overlay mechanics (issue #10).

All Win32 surface is exercised through injected fakes or monkeypatched
module globals, so these tests run on any platform without a display.
"""

from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pyglet

pyglet.options["shadow_window"] = False

from client.system import fullscreen, window_manager  # noqa: E402
from client.system.dirty_tracker import DirtyTracker  # noqa: E402
from client.system.dpi import (  # noqa: E402
    declare_per_monitor_v2_dpi_awareness,
    dpi_scale_factor,
    parse_wm_dpichanged,
    window_dpi,
)
from client.system.input_router import (  # noqa: E402
    InputRouter,
    screen_to_window_coords,
)


def _make_monitor_user32(monitor_rect: tuple) -> MagicMock:
    user32 = MagicMock(name="user32")

    def get_info(monitor: int, ptr: int) -> int:
        info = ctypes.cast(ptr, ctypes.POINTER(fullscreen._MonitorInfo)).contents
        rc = info.rcMonitor
        rc.left, rc.top, rc.right, rc.bottom = monitor_rect
        return 1

    user32.MonitorFromWindow.return_value = 777
    user32.GetMonitorInfoW.side_effect = get_info
    return user32


def _make_manager(monkeypatch: pytest.MonkeyPatch):
    mgr = window_manager.WindowsWindowManager.__new__(
        window_manager.WindowsWindowManager
    )
    mgr.hwnd = 0xABCD
    mgr._click_through = True
    mgr._dpi = None
    mgr._current_opacity = 255
    fake_windll = SimpleNamespace(user32=MagicMock(name="user32"))
    monkeypatch.setattr(window_manager, "windll", fake_windll)
    return mgr, fake_windll.user32


def _make_soul(x: int, y: int, w: int = 100, h: int = 100) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y, width=w, height=h)


class TestMonitorRect:
    def test_monitor_rect_for_window(self) -> None:
        user32 = _make_monitor_user32((10, 20, 1930, 1100))
        assert fullscreen.monitor_rect_for_window(0x100, user32=user32) == (
            10,
            20,
            1930,
            1100,
        )


class TestDpi:
    def test_scale_factor(self) -> None:
        assert dpi_scale_factor(96) == 1.0
        assert dpi_scale_factor(120) == 1.25
        assert dpi_scale_factor(144) == 1.5
        assert dpi_scale_factor(192) == 2.0

    def test_parse_wm_dpichanged(self) -> None:
        change = parse_wm_dpichanged((144 << 16) | 144, (0, 0, 3840, 2160))
        assert change.dpi_x == 144
        assert change.dpi_y == 144
        assert change.scale == 1.5
        assert change.suggested_rect == (0, 0, 3840, 2160)

    def test_parse_wm_dpichanged_asymmetric(self) -> None:
        change = parse_wm_dpichanged((120 << 16) | 96, (5, 5, 100, 100))
        assert (change.dpi_x, change.dpi_y) == (96, 120)
        assert change.scale == 1.0

    def test_declare_dpi_awareness_uses_v2_context(self) -> None:
        user32 = MagicMock(name="user32")
        user32.SetProcessDpiAwarenessContext.return_value = 1
        assert declare_per_monitor_v2_dpi_awareness(user32=user32)
        (context,), _ = user32.SetProcessDpiAwarenessContext.call_args
        assert context.value == ctypes.c_void_p(-4).value

    def test_declare_dpi_awareness_no_win32(self) -> None:
        if sys.platform == "win32":
            pytest.skip("win32-only guard not exercisable here")
        assert not declare_per_monitor_v2_dpi_awareness()

    def test_window_dpi(self) -> None:
        user32 = MagicMock(name="user32")
        user32.GetDpiForWindow.return_value = 144
        assert window_dpi(0xABCD, user32=user32) == 144

    def test_window_dpi_zero_is_none(self) -> None:
        user32 = MagicMock(name="user32")
        user32.GetDpiForWindow.return_value = 0
        assert window_dpi(0xABCD, user32=user32) is None

    def test_window_dpi_no_win32(self) -> None:
        if sys.platform == "win32":
            pytest.skip("win32-only guard not exercisable here")
        assert window_dpi(0xABCD) is None


class TestDirtyTracker:
    def test_starts_dirty(self) -> None:
        assert DirtyTracker().is_dirty

    def test_consume_clears_flag(self) -> None:
        tracker = DirtyTracker()
        assert tracker.consume()
        assert not tracker.consume()
        assert not tracker.is_dirty

    def test_mark_dirty_rearms(self) -> None:
        tracker = DirtyTracker()
        assert tracker.consume()
        tracker.mark_dirty()
        assert tracker.is_dirty
        assert tracker.consume()

    def test_zero_flip_idle(self) -> None:
        tracker = DirtyTracker()
        draws = 0
        for _ in range(600):
            if tracker.consume():
                draws += 1
        assert draws == 1


class TestInputRouterPoll:
    def test_screen_to_window_coords(self) -> None:
        assert screen_to_window_coords(100, 200, 1080) == (100, 880)

    def test_poll_hit_over_soul(self) -> None:
        router = InputRouter()
        soul = _make_soul(50, 50)
        found = router.poll_soul_under_cursor([soul], (75, 100), 1080)
        assert found is soul

    def test_poll_miss_over_empty_space(self) -> None:
        router = InputRouter()
        soul = _make_soul(50, 50)
        assert router.poll_soul_under_cursor([soul], (900, 900), 1080) is None

    def test_poll_returns_topmost_soul(self) -> None:
        router = InputRouter()
        bottom = _make_soul(50, 50)
        top = _make_soul(60, 60)
        found = router.poll_soul_under_cursor([bottom, top], (100, 100), 1080)
        assert found is top


class TestViewportHoverChain:
    def test_stream_soul_hover_hits(self) -> None:
        """Hub snapshot -> consumer -> mapper -> Soul placement (as
        _update_viewport_souls does) -> hover hit-test (as
        _poll_click_through does). A cursor on the visual center hits."""
        from client.core.soul.soul import Soul
        from client.system.network.viewport_client import (
            ViewportConsumer,
            ViewportMapper,
        )

        soul_id = "de67211ae6aaff916c2bd660adac87a6"
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(
            {
                "type": "snapshot",
                "souls": [
                    {
                        "soul_id": soul_id,
                        "x": 1020.0,
                        "y": 540.0,
                        "state": "normal",
                        "dormant": False,
                    }
                ],
                "wallets": [],
                "abroad": [],
                "region": {"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0},
            }
        )
        mapper = ViewportMapper()
        mapper.set_region(*consumer.region)
        positions = consumer.rendered_positions(now=1000.0)
        soul = Soul(
            orb_color_rgb=(1.0, 0.0, 0.0),
            aura_color_rgb=(0.0, 1.0, 0.0),
            name="Hub Soul",
            initial_position=(1020.0, 540.0),
            screen_width=1920,
            screen_height=1080,
            owner_id="hub",
            local_instance_id="me",
        )
        try:
            wx, wy = positions[soul_id]
            sx, sy = mapper.world_to_screen(wx, wy, 1920, 1080)
            soul.x, soul.y, soul.draw_y = sx, sy, sy
            router = InputRouter()
            cursor = (sx + soul.width / 2, sy + soul.height / 2)
            assert router.poll_soul_under_cursor([soul], cursor, 1080) is soul
        finally:
            soul.cleanup()


class TestClickThrough:
    def test_enable_sets_transparent(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        user32.GetWindowLongW.return_value = 0x100
        assert mgr.set_click_through(True)
        user32.SetWindowLongW.assert_called_once_with(0xABCD, -20, 0x100 | 0x20)

    def test_disable_clears_transparent(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        user32.GetWindowLongW.return_value = 0x120
        assert mgr.set_click_through(False)
        user32.SetWindowLongW.assert_called_once_with(0xABCD, -20, 0x100)

    def test_no_hwnd_returns_false(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        mgr.hwnd = None
        assert not mgr.set_click_through(True)
        user32.SetWindowLongW.assert_not_called()

    def test_update_click_through_makes_clickable_over_soul(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        user32.GetWindowLongW.return_value = 0x120
        assert mgr.update_click_through(_make_soul(0, 0)) is False
        user32.SetWindowLongW.assert_called_once_with(0xABCD, -20, 0x100)

    def test_update_click_through_idempotent(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        user32.GetWindowLongW.return_value = 0x120
        mgr.update_click_through(_make_soul(0, 0))
        mgr.update_click_through(_make_soul(10, 10))
        assert user32.SetWindowLongW.call_count == 1

    def test_update_click_through_stays_through_on_empty(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        assert mgr.update_click_through(None) is True
        user32.SetWindowLongW.assert_not_called()

    def test_apply_transparency_sets_noactivate_and_through(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        user32.GetWindowLongW.return_value = 0x0
        assert mgr._apply_transparency()
        style = 0x0 | mgr.WS_EX_LAYERED | mgr.WS_EX_NOACTIVATE | mgr.WS_EX_TRANSPARENT
        user32.SetWindowLongW.assert_called_once_with(0xABCD, -20, style)


class TestFocusPolicy:
    def test_show_window_uses_showna(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        user32.ShowWindow.return_value = 1
        assert mgr.show_window()
        user32.ShowWindow.assert_called_once_with(0xABCD, mgr.SW_SHOWNA)
        assert mgr.SW_SHOWNA == 8

    def test_always_on_top_keeps_noactivate(self) -> None:
        flags = (
            window_manager.WindowsWindowManager.SWP_NOMOVE
            | window_manager.WindowsWindowManager.SWP_NOSIZE
            | window_manager.WindowsWindowManager.SWP_NOACTIVATE
            | window_manager.WindowsWindowManager.SWP_FRAMECHANGED
        )
        assert flags & window_manager.WindowsWindowManager.SWP_NOACTIVATE


class TestDpiHandling:
    def test_check_dpi_changed(self, monkeypatch) -> None:
        mgr, _ = _make_manager(monkeypatch)
        dpis = iter([144, 144, 192])
        monkeypatch.setattr(window_manager, "window_dpi", lambda hwnd: next(dpis))
        applied = []
        monkeypatch.setattr(mgr, "apply_monitor_rect", lambda: applied.append(True))
        assert not mgr.check_dpi_changed()
        assert mgr._dpi == 144
        assert not mgr.check_dpi_changed()
        assert not applied
        assert mgr.check_dpi_changed()
        assert mgr._dpi == 192
        assert applied == [True]

    def test_check_dpi_changed_no_dpi(self, monkeypatch) -> None:
        mgr, _ = _make_manager(monkeypatch)
        monkeypatch.setattr(window_manager, "window_dpi", lambda hwnd: None)
        assert not mgr.check_dpi_changed()

    def test_on_wm_dpichanged_applies_suggested_rect(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        wparam = (144 << 16) | 144
        assert mgr.on_wm_dpichanged(wparam, (10, 20, 100, 200))
        assert mgr._dpi == 144
        flags = mgr.SWP_NOZORDER | mgr.SWP_NOACTIVATE | mgr.SWP_FRAMECHANGED
        user32.SetWindowPos.assert_called_once_with(
            0xABCD, None, 10, 20, 90, 180, flags
        )

    def test_apply_monitor_rect(self, monkeypatch) -> None:
        mgr, user32 = _make_manager(monkeypatch)
        monkeypatch.setattr(
            window_manager,
            "monitor_rect_for_window",
            lambda hwnd: (0, 0, 1920, 1080),
        )
        assert mgr.apply_monitor_rect()
        flags = mgr.SWP_NOZORDER | mgr.SWP_NOACTIVATE | mgr.SWP_FRAMECHANGED
        user32.SetWindowPos.assert_called_once_with(
            0xABCD, None, 0, 0, 1920, 1080, flags
        )

    def test_get_cursor_pos_guarded_off_windows(self) -> None:
        if sys.platform == "win32":
            pytest.skip("win32-only guard not exercisable here")
        assert window_manager.get_cursor_pos() is None
