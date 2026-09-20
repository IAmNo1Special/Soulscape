"""Regression tests for idle-soul animation starvation.

Bug: the dirty-gated render loop only redrew when a soul's rounded
position changed. An idle soul's shader uniforms (plasma pulse, hover
bob, camera orbit) advance every frame via visual_tick, but with no
position change no redraw was scheduled -- the soul froze on one
frame until an unrelated event (e.g. mouse hover) marked the scene
dirty.

These tests drive the real frame_needs_redraw decision against real
Soul objects through the exact reported scenario: same position on
consecutive frames.

pyglet needs a display, so we stub pyglet/pyglet.gl in sys.modules
before importing the Soul class. frame_needs_redraw itself is pure.
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


def _install_pyglet_stubs() -> dict[str, types.ModuleType | None]:
    """Swap pyglet/pyglet.gl for headless stubs; returns prior state."""
    prior = {name: sys.modules.get(name) for name in ("pyglet", "pyglet.gl")}
    for name in prior:
        sys.modules[name] = _StubModule(name)
    return prior


def _restore_pyglet(prior: dict[str, types.ModuleType | None]) -> None:
    """Restore the pre-stub sys.modules so later test modules see real pyglet."""
    for name, mod in prior.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


_prior_pyglet = _install_pyglet_stubs()
try:
    from client.core.soul.soul import Soul  # noqa: E402
    from client.system.dirty_tracker import frame_needs_redraw  # noqa: E402
finally:
    _restore_pyglet(_prior_pyglet)


@pytest.fixture
def soul() -> Soul:
    return Soul(
        orb_color_rgb=(0.5, 0.5, 0.5),
        aura_color_rgb=(1.0, 0.5, 0.0),
        task_scheduler=MagicMock(),
    )


def _two_frames(soul: Soul, sim_paused: bool = False) -> tuple[bool, bool]:
    """Run the redraw decision for two consecutive idle frames."""
    first, snapshot = frame_needs_redraw([soul], sim_paused, None)
    second, _ = frame_needs_redraw([soul], sim_paused, snapshot)
    return first, second


def test_idle_animated_soul_redraws_every_frame(soul: Soul) -> None:
    first, second = _two_frames(soul)
    assert first is True
    assert second is True


def test_statue_soul_does_not_force_redraw(soul: Soul) -> None:
    soul.statue = True
    first, second = _two_frames(soul)
    assert first is True
    assert second is False


def test_dormant_statue_soul_does_not_force_redraw(soul: Soul) -> None:
    soul.dormant_statue = True
    first, second = _two_frames(soul)
    assert first is True
    assert second is False


def test_paused_sim_does_not_force_redraw(soul: Soul) -> None:
    first, second = _two_frames(soul, sim_paused=True)
    assert first is True
    assert second is False


def test_moved_soul_redraws_even_when_statue(soul: Soul) -> None:
    soul.statue = True
    first, snapshot = frame_needs_redraw([soul], False, None)
    assert first is True
    soul.x += 1.0
    second, _ = frame_needs_redraw([soul], False, snapshot)
    assert second is True
