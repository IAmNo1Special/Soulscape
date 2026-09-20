"""Contract tests for scene_renderer._soul_state against real Soul objects.

Regression: scene_renderer called biology.max_health() as if it were a
method, but SoulBiology.max_health is a property, crashing every frame
(TypeError: 'int' object is not callable). Existing tests only mirrored
the math instead of calling the real function, so nothing caught it.

pyglet needs a display, so we stub pyglet/pyglet.gl in sys.modules
before importing scene_renderer. _soul_state itself never touches GL.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


class _StubModule(types.ModuleType):
    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _dummy


def _dummy(*args, **kwargs):
    return None


def _install_pyglet_stubs() -> None:
    for mod_name in ("pyglet", "pyglet.gl"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = _StubModule(mod_name)


_install_pyglet_stubs()

from client.core.soul.soul import Soul  # noqa: E402
from client.ui.graphics.scene_renderer import _soul_state  # noqa: E402


@pytest.fixture
def soul() -> Soul:
    return Soul(
        orb_color_rgb=(0.5, 0.5, 0.5),
        aura_color_rgb=(1.0, 0.5, 0.0),
        task_scheduler=MagicMock(),
    )


def test_soul_state_hp_is_fraction(soul: Soul) -> None:
    state = _soul_state(soul, soul.display_orb_color())
    expected = soul.biology.get_current_health() / max(1, soul.biology.max_health)
    assert state["hp"] == pytest.approx(expected)
    assert 0.0 <= state["hp"] <= 1.0


def test_soul_state_reads_max_health_as_property(soul: Soul) -> None:
    max_health = soul.biology.max_health
    assert isinstance(max_health, int)
    state = _soul_state(soul, soul.display_orb_color())
    assert state["hp"] == pytest.approx(
        soul.biology.get_current_health() / max(1, max_health)
    )
