"""Regression tests for two viewport-mode bugs found live 2026-09-20.

Bug 1: the first Hub stream frame carrying a soul crashed the client.
``_update_viewport_souls`` assigned ``soul.biology.stats.max_hp``, but
``max_hp`` was a read-only derived property::

    AttributeError: property 'max_hp' of 'SoulStats' object has no setter

In hub mode no soul could ever render -- the client died on the first
streamed soul.

Bug 2: a locally spawned soul was pruned by the viewport reconcile loop
on the very next frame, because the Hub had not echoed it back yet.
The log showed "Spawned new soul" followed a second later by
"Cleaned up resources" -- the soul flickered out of existence until
its Hub echo arrived as a stranger ("Hub Soul <id>").

pyglet needs a display, so we stub pyglet/pyglet.gl in sys.modules
before importing the Soul class, then restore the prior state so later
test modules are unaffected.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from shared.enums import Stat

from client.system.network.viewport_client import viewport_prune_ids


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
    """Restore the pre-stub sys.modules so later test modules are clean."""
    for name, mod in prior.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


_prior_pyglet = _install_pyglet_stubs()
try:
    from client.core.soul.soul import Soul  # noqa: E402
finally:
    _restore_pyglet(_prior_pyglet)


@pytest.fixture
def soul() -> Soul:
    return Soul(
        orb_color_rgb=(0.5, 0.5, 0.5),
        aura_color_rgb=(1.0, 0.5, 0.0),
        task_scheduler=MagicMock(),
    )


def _apply_stream_biology(soul: Soul, bio: dict) -> None:
    """The exact biology application from _update_viewport_souls."""
    soul.biology.satiety = bio["satiety"]
    soul.biology.hydration = bio["hydration"]
    soul.biology.stats.max_hp = max(1, int(round(bio["max_hp"])))
    soul.biology.current_health = max(
        0,
        min(soul.biology.stats.max_hp, int(round(bio["hp"]))),
    )


def test_streamed_max_hp_applies_without_crash(soul: Soul) -> None:
    _apply_stream_biology(
        soul,
        {"satiety": 30.0, "hydration": 40.0, "hp": 50.0, "max_hp": 120.0},
    )
    assert soul.biology.stats.max_hp == 120
    assert soul.biology.current_health == 50
    assert soul.biology.max_health == 120
    assert soul.biology.satiety == 30.0
    assert soul.biology.hydration == 40.0


def test_streamed_hp_clamps_to_streamed_max_hp(soul: Soul) -> None:
    _apply_stream_biology(
        soul,
        {"satiety": 100.0, "hydration": 100.0, "hp": 999.0, "max_hp": 80.0},
    )
    assert soul.biology.stats.max_hp == 80
    assert soul.biology.current_health == 80


def test_max_hp_still_derived_without_override(soul: Soul) -> None:
    assert soul.biology.stats.max_hp == soul.biology.stats.calculate_value(Stat.HP)


def test_max_hp_override_does_not_leak_into_derived_stats(
    soul: Soul,
) -> None:
    derived = soul.biology.stats.calculate_value(Stat.HP)
    soul.biology.stats.max_hp = derived + 25
    assert soul.biology.stats.max_hp == derived + 25
    assert soul.biology.stats.calculate_value(Stat.HP) == derived


def test_max_hp_override_clamps_to_one(soul: Soul) -> None:
    soul.biology.stats.max_hp = 0
    assert soul.biology.stats.max_hp == 1


def _owned_soul(owner_id: str = "owner-1") -> Soul:
    return Soul(
        orb_color_rgb=(0.5, 0.5, 0.5),
        aura_color_rgb=(1.0, 0.5, 0.0),
        owner_id=owner_id,
        local_instance_id="owner-1",
        task_scheduler=MagicMock(),
    )


def test_locally_owned_soul_survives_missing_hub_echo() -> None:
    soul = _owned_soul()
    existing = {soul.biology.soul_id: soul}
    assert viewport_prune_ids(existing, set(), "owner-1", set()) == []


def test_hub_owned_soul_pruned_when_absent_from_stream() -> None:
    soul = _owned_soul(owner_id="hub")
    sid = soul.biology.soul_id
    existing = {sid: soul}
    assert viewport_prune_ids(existing, set(), "owner-1", set()) == [sid]


def test_walkoff_fade_soul_not_pruned() -> None:
    soul = _owned_soul(owner_id="hub")
    sid = soul.biology.soul_id
    existing = {sid: soul}
    assert viewport_prune_ids(existing, set(), "owner-1", {sid}) == []


def test_streamed_soul_not_pruned() -> None:
    soul = _owned_soul(owner_id="hub")
    sid = soul.biology.soul_id
    existing = {sid: soul}
    assert viewport_prune_ids(existing, {sid}, "owner-1", set()) == []


def test_prune_result_is_deterministic_across_calls() -> None:
    first = _owned_soul(owner_id="hub")
    second = _owned_soul(owner_id="hub")
    existing = {
        first.biology.soul_id: first,
        second.biology.soul_id: second,
    }
    once = viewport_prune_ids(existing, set(), "owner-1", set())
    twice = viewport_prune_ids(existing, set(), "owner-1", set())
    assert sorted(once) == sorted(twice)
    assert len(once) == 2
