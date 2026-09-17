"""Headless tests for the tray dashboard (issue #30).

pystray is stubbed in sys.modules before importing the controller, so
menu construction and every callback are exercised without a display.
"""

from __future__ import annotations

import sys
import types
import unittest


def _install_fake_pystray():
    fake = types.ModuleType("pystray")

    class MenuItem:
        def __init__(self, text, action=None, checked=None, enabled=True,
                     **kw):
            self.text = text
            self.action = action
            self.checked = checked
            self.enabled = enabled

    class Menu(tuple):
        SEPARATOR = object()

        def __new__(cls, *items):
            return tuple.__new__(cls, items)

    Menu.SEPARATOR = object()

    class Icon:
        def __init__(self, name, image, title, menu):
            self.name = name
            self.menu = menu
            self.notified: list[tuple[str, str]] = []
            self.updated = 0
            self.stopped = False

        def run(self):
            pass

        def stop(self):
            self.stopped = True

        def notify(self, message, title=""):
            self.notified.append((message, title))

        def update_menu(self):
            self.updated += 1

    fake.Menu = Menu
    fake.MenuItem = MenuItem
    fake.Icon = Icon
    sys.modules["pystray"] = fake
    return fake


_install_fake_pystray()

from client.system.tray import TrayController  # noqa: E402


def _souls():
    return [
        {
            "soul_id": "s1",
            "name": "Zed",
            "essence": 42.5,
            "location": "Commons (plot 8:4)",
            "state": "normal",
        },
        {
            "soul_id": "s2",
            "name": "Mira",
            "essence": None,
            "location": "traveling...",
            "state": "traveling",
        },
    ]


def _menu_texts(menu):
    out = []
    for item in menu:
        if item is type(menu).SEPARATOR:
            continue
        text = item.text() if callable(item.text) else item.text
        out.append(text)
    return out


def _find(menu, text):
    sep = sys.modules["pystray"].Menu.SEPARATOR
    for item in menu:
        if item is sep:
            continue
        t = item.text() if callable(item.text) else item.text
        if t == text:
            return item
    raise AssertionError(f"menu item {text!r} not found in {_menu_texts(menu)}")


class TestTrayDashboard(unittest.TestCase):
    def _controller(self, **kw):
        args = {
            "get_souls": _souls,
            "get_hub_status": lambda: "online",
            "get_mailbag_count": lambda: 3,
        }
        args.update(kw)
        return TrayController(
            on_add_soul=lambda: None,
            on_toggle_auras=lambda: None,
            on_settings=lambda: None,
            on_message_board=lambda: None,
            on_exit=lambda: None,
            **args,
        )

    def test_top_level_dashboard_items(self):
        ctrl = self._controller()
        texts = _menu_texts(ctrl._create_menu())
        for expected in (
            "Status", "Wallet", "Mailbag (3)",
            "Whereabouts", "Quips", "Add Soul", "Toggle All Auras",
            "Pause simulation", "Work mode", "Social / Message Board",
            "Market (no client surface yet)", "Settings...",
            "Exit Soulscape",
        ):
            self.assertIn(expected, texts)

    def test_mailbag_item_enabled_and_click_opens_surface(self):
        opened = []
        ctrl = self._controller(on_open_mailbag=lambda: opened.append(True))
        item = _find(ctrl._create_menu(), "Mailbag (3)")
        self.assertTrue(item.enabled)
        item.action(None, item)
        self.assertEqual(opened, [True])

    def test_mailbag_item_disabled_without_surface(self):
        ctrl = self._controller()
        item = _find(ctrl._create_menu(), "Mailbag (3)")
        self.assertFalse(item.enabled)

    def test_whereabouts_shows_location_strings(self):
        ctrl = self._controller()
        where = _find(ctrl._create_menu(), "Whereabouts")
        texts = _menu_texts(where.action)
        self.assertIn("Zed — Commons (plot 8:4)", texts)
        self.assertIn("Mira — traveling...", texts)

    def test_wallet_shows_essence(self):
        ctrl = self._controller()
        wallet = _find(ctrl._create_menu(), "Wallet")
        texts = _menu_texts(wallet.action)
        self.assertIn("Zed — 42.5 essence", texts)
        self.assertIn("Mira — ?", texts)

    def test_status_shows_counts(self):
        ctrl = self._controller()
        status = _find(ctrl._create_menu(), "Status")
        texts = _menu_texts(status.action)
        self.assertIn("Souls: 2", texts)
        self.assertIn("Hub: online", texts)

    def test_market_disabled_without_surface(self):
        ctrl = self._controller()
        item = _find(ctrl._create_menu(), "Market (no client surface yet)")
        self.assertFalse(item.enabled)

    def test_pause_toggle(self):
        calls = []
        state = {"paused": False}
        ctrl = self._controller(
            is_paused=lambda: state["paused"],
            on_pause_toggle=lambda: (calls.append(1),
                                      state.__setitem__("paused", True)),
        )
        item = _find(ctrl._create_menu(), "Pause simulation")
        self.assertFalse(item.checked(item))
        ctrl._on_pause_toggle(None, item)
        self.assertEqual(calls, [1])
        self.assertTrue(item.checked(item))

    def test_work_mode_toggle(self):
        calls = []
        state = {"wm": False}
        ctrl = self._controller(
            is_work_mode=lambda: state["wm"],
            on_work_mode_toggle=lambda: (calls.append(1),
                                          state.__setitem__("wm", True)),
        )
        item = _find(ctrl._create_menu(), "Work mode")
        ctrl._on_work_mode_toggle(None, item)
        self.assertEqual(calls, [1])

    def test_quip_menu_lists_souls(self):
        ctrl = self._controller(on_request_quip=lambda sid: {"status": "success"})
        quips = _find(ctrl._create_menu(), "Quips")
        texts = _menu_texts(quips.action)
        self.assertIn("Ask Zed for a quip", texts)
        self.assertIn("Ask Mira for a quip", texts)

    def test_quip_rejection_surfaces(self):
        bubbled = []
        ctrl = self._controller(
            on_request_quip=lambda sid: {
                "status": "rejected",
                "reason": "budget_exhausted",
                "message": "Quip budget exhausted.",
            },
            notify_bubble=lambda sid, text, kind: bubbled.append((sid, text, kind)),
        )
        ctrl.icon = _FakeIcon([])
        notified = ctrl.icon._notified
        quips = _find(ctrl._create_menu(), "Quips")
        item = _find(quips.action, "Ask Zed for a quip")
        item.action(None, item)
        self.assertEqual(len(notified), 1)
        self.assertIn("budget", notified[0][0])
        self.assertEqual(bubbled, [("s1", "Quip budget exhausted.", "system")])

    def test_quip_success_silent(self):
        bubbled = []
        ctrl = self._controller(
            on_request_quip=lambda sid: {"status": "success"},
            notify_bubble=lambda sid, text, kind: bubbled.append((sid, text, kind)),
        )
        ctrl.icon = _FakeIcon([])
        quips = _find(ctrl._create_menu(), "Quips")
        item = _find(quips.action, "Ask Zed for a quip")
        item.action(None, item)
        self.assertEqual(ctrl.icon._notified, [])
        self.assertEqual(bubbled, [])

    def test_legacy_callbacks_still_fire(self):
        calls = []
        ctrl = TrayController(
            on_add_soul=lambda: calls.append("add"),
            on_toggle_auras=lambda: calls.append("auras"),
            on_settings=lambda: calls.append("settings"),
            on_message_board=lambda: calls.append("board"),
            on_exit=lambda: calls.append("exit"),
        )
        menu = ctrl._create_menu()
        _find(menu, "Add Soul").action(None, None)
        _find(menu, "Toggle All Auras").action(None, None)
        _find(menu, "Settings...").action(None, None)
        _find(menu, "Social / Message Board").action(None, None)
        self.assertEqual(calls, ["add", "auras", "settings", "board"])


class _FakeIcon:
    def __init__(self, notified):
        self._notified = notified

    def notify(self, message, title=""):
        self._notified.append((message, title))

    def update_menu(self):
        pass


if __name__ == "__main__":
    unittest.main()
