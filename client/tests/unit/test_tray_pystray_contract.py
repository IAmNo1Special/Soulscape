"""Pystray constructor-contract tests for the tray menu.

Real pystray validates every MenuItem action at construction time
(pystray._base.MenuItem._assert_action): the callable must take 0, 1,
or 2 positional parameters, anything else raises ValueError. The
headless stub used by test_tray_dashboard.py performs no such
validation, so a three-parameter ``lambda icon, item, sid=sid``
passed CI and then crashed the real client during tray setup.

These tests build the menus with the REAL pystray.Menu / MenuItem
(dummy backend, no display required) so the constructor contract is
enforced exactly as in production. Skipped when pystray is not
installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("PYSTRAY_BACKEND", "dummy")


def _load_real_pystray():
    """Import the real pystray even when a stub occupies sys.modules.

    The real module is returned as a private reference; sys.modules is
    left exactly as it was found so the other test modules keep working
    with their own stub.
    """
    stub = sys.modules.pop("pystray", None)
    try:
        real = importlib.import_module("pystray")
    except ImportError:
        real = None
    finally:
        sys.modules.pop("pystray", None)
        if stub is not None:
            sys.modules["pystray"] = stub
    return real


_real_pystray = _load_real_pystray()
if _real_pystray is None:
    pytest.skip("pystray is not installed", allow_module_level=True)


def _load_tray_with_real_pystray():
    """Import tray.py as a private module bound to the real pystray.

    Uses a dedicated module name so sys.modules["client.system.tray"]
    -- and whichever pystray stub the other test modules installed --
    is left untouched.
    """
    stub = sys.modules.get("pystray")
    module = types.ModuleType("pystray")
    module.Menu = _real_pystray.Menu
    module.MenuItem = _real_pystray.MenuItem
    module.Icon = _real_pystray.Icon
    sys.modules["pystray"] = module
    try:
        path = Path(__file__).resolve().parents[2] / "system" / "tray.py"
        spec = importlib.util.spec_from_file_location(
            "tray_pystray_contract_under_test", path
        )
        tray = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray)
        return tray
    finally:
        if stub is None:
            sys.modules.pop("pystray", None)
        else:
            sys.modules["pystray"] = stub


TrayController = _load_tray_with_real_pystray().TrayController


def _souls():
    return [
        {
            "soul_id": "s1",
            "name": "Zed",
            "essence": 42.5,
            "location": "Commons",
            "state": "normal",
        },
        {
            "soul_id": "s2",
            "name": "Aya",
            "essence": 7.0,
            "location": "home",
            "state": "normal",
        },
    ]


def _recaps():
    return {
        "s1": [
            {"soul_id": "s1", "day": "2026-09-19", "lines": ["a", "b"]},
            {"soul_id": "s1", "day": "2026-09-18", "lines": ["c"]},
        ],
        "s2": [
            {"soul_id": "s2", "day": "2026-09-19", "lines": ["d"]},
        ],
    }


def _controller(quip_calls, recap_calls):
    def on_request_quip(soul_id):
        quip_calls.append(soul_id)
        return {"status": "success"}

    def on_open_recap(soul_id, day):
        recap_calls.append((soul_id, day))

    return TrayController(
        on_add_soul=lambda: None,
        on_toggle_auras=lambda: None,
        on_settings=lambda: None,
        on_message_board=lambda: None,
        on_exit=lambda: None,
        get_souls=_souls,
        on_request_quip=on_request_quip,
        get_recaps=_recaps,
        on_open_recap=on_open_recap,
    )


def _items(menu):
    return [i for i in menu if isinstance(i, _real_pystray.MenuItem)]


def _find(menu, text):
    for item in _items(menu):
        if item.text == text:
            return item
    raise AssertionError(f"menu item {text!r} not found")


def _submenu(item):
    action = item._action
    assert isinstance(action, _real_pystray.Menu), "expected a submenu"
    return action


def test_create_menu_satisfies_pystray_action_contract():
    """Building the full menu must not raise (crashed the real client)."""
    tc = _controller([], [])
    tc._create_menu()


def test_quip_actions_carry_their_own_soul_id():
    quip_calls: list = []
    tc = _controller(quip_calls, [])
    quips = _submenu(_find(tc._create_menu(), "Quips"))
    for item in _items(quips):
        item(object())
    assert quip_calls == ["s1", "s2"]


def test_recap_actions_carry_soul_id_and_day():
    recap_calls: list = []
    tc = _controller([], recap_calls)
    recap_menu = _submenu(_find(tc._create_menu(), "Morning recap"))
    for soul_item in _items(recap_menu):
        for day_item in _items(_submenu(soul_item)):
            day_item(object())
    assert recap_calls == [
        ("s1", "2026-09-19"),
        ("s1", "2026-09-18"),
        ("s2", "2026-09-19"),
    ]
