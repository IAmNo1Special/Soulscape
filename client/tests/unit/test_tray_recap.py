"""Headless tests for the morning-recap tray dashboard view (issue #33).

pystray is stubbed in sys.modules before importing the controller, so
menu construction and every callback are exercised without a display.
All Hub data is fake -- the point is the browse interaction, not the
wire format (covered by the server tests + test_recap_client.py).
"""

from __future__ import annotations

import sys
import types
import unittest


def _install_fake_pystray():
    # Reuse the stub installed by test_tray_dashboard.py when both files
    # run in one pytest process -- clobbering it breaks SEPARATOR identity
    # and the richer Icon API in those tests.
    if "pystray" in sys.modules:
        return sys.modules["pystray"]
    fake = types.ModuleType("pystray")

    class MenuItem:
        def __init__(self, text, action=None, checked=None, enabled=True, **kw):
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
        {"soul_id": "s1", "name": "Zed", "essence": 42.5},
        {"soul_id": "s2", "name": "Mira", "essence": 10.0},
    ]


def _recaps():
    return {
        "s1": [
            {
                "soul_id": "s1",
                "day": "2026-09-16",
                "lines": [
                    {"text": "Loyalty +0.03 (petting): 0.50→0.53", "cites": []},
                    {"text": "Answered: What do you dream about?", "cites": []},
                ],
                "generated_at": 1758000000.0,
                "shown_at": 1758000060.0,
            },
            {
                "soul_id": "s1",
                "day": "2026-09-15",
                "lines": [{"text": "Woke from dormancy", "cites": []}],
                "generated_at": 1757913600.0,
                "shown_at": None,
            },
        ],
        "s2": [],
    }


def _menu_texts(menu):
    out = []
    for item in menu:
        if item is type(menu).SEPARATOR:
            continue
        text = item.text() if callable(item.text) else item.text
        out.append(text)
    return out


def _find(menu, text):
    for item in menu:
        t = item.text() if callable(item.text) else item.text
        if t == text:
            return item
    raise AssertionError(f"menu item {text!r} not found in {_menu_texts(menu)}")


class TestRecapDashboard(unittest.TestCase):
    def _controller(self, **kw):
        args = {
            "get_souls": _souls,
            "get_recaps": _recaps,
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

    def test_morning_recap_in_top_level_menu(self):
        ctrl = self._controller()
        self.assertIn("Morning recap", _menu_texts(ctrl._create_menu()))

    def test_recap_menu_lists_souls_and_days(self):
        ctrl = self._controller()
        submenu = _find(ctrl._create_menu(), "Morning recap").action
        self.assertIn("Zed", _menu_texts(submenu))
        zed_days = _find(submenu, "Zed").action
        texts = _menu_texts(zed_days)
        self.assertIn("2026-09-16 — 2 highlights", texts)
        self.assertIn("2026-09-15 — 1 highlight", texts)

    def test_clicking_day_opens_recap(self):
        opened = []
        ctrl = self._controller(
            on_open_recap=lambda sid, day: opened.append((sid, day))
        )
        submenu = _find(ctrl._create_menu(), "Morning recap").action
        zed_days = _find(submenu, "Zed").action
        item = _find(zed_days, "2026-09-16 — 2 highlights")
        self.assertTrue(item.enabled)
        item.action(None, item)
        self.assertEqual(opened, [("s1", "2026-09-16")])

    def test_no_recaps_shows_disabled_slot(self):
        ctrl = self._controller(get_recaps=lambda: {})
        submenu = _find(ctrl._create_menu(), "Morning recap").action
        texts = _menu_texts(submenu)
        self.assertEqual(texts, ["no recaps yet"])

    def test_day_items_disabled_without_opener(self):
        ctrl = self._controller()
        submenu = _find(ctrl._create_menu(), "Morning recap").action
        zed_days = _find(submenu, "Zed").action
        item = _find(zed_days, "2026-09-16 — 2 highlights")
        self.assertFalse(item.enabled)


if __name__ == "__main__":
    unittest.main()
