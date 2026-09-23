"""Unit tests for the issue #21 statue render path.

Pure logic, no GL context: the viewport consumer must record the Hub
lifecycle state (snapshot + state-only deltas), and the Soul must render
collapsed souls as desaturated, dimmed stone while normal souls keep
their live colors. The scene renderer reads Soul.display_orb_color() /
display_aura_color(), so those are the contract under test.
"""

from __future__ import annotations

import unittest

from shared import protocol

from client.core.soul.soul import Soul
from client.system.network.viewport_client import (
    STATUE_STATE,
    ViewportConsumer,
    is_statue,
    statue_orb_color,
)


def make_snapshot(souls, seq=0):
    return protocol.envelope(
        protocol.MessageType.SNAPSHOT,
        seq=seq,
        tick_id=1,
        souls=souls,
    )


def make_delta(ops, seq=1):
    return protocol.envelope(protocol.MessageType.DELTA, seq=seq, tick_id=1, ops=ops)


def state_op(soul_id, state):
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "state": {"soul_id": soul_id, "state": state},
    }


def upsert_op(soul_id, x, y, snap=False):
    op = {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "state": {"soul_id": soul_id, "x": x, "y": y},
    }
    if snap:
        op["snap"] = True
    return op


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestConsumerStateTracking(unittest.TestCase):
    def test_snapshot_records_state_per_soul(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(
            make_snapshot(
                [
                    {"soul_id": "a", "x": 1.0, "y": 2.0, "state": "collapsed"},
                    {"soul_id": "b", "x": 3.0, "y": 4.0, "state": "normal"},
                ]
            )
        )
        self.assertEqual(consumer.soul_state("a"), "collapsed")
        self.assertEqual(consumer.soul_state("b"), "normal")
        self.assertEqual(consumer.soul_states(), {"a": "collapsed", "b": "normal"})

    def test_missing_state_defaults_normal(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(make_snapshot([{"soul_id": "a", "x": 1.0, "y": 2.0}]))
        self.assertEqual(consumer.soul_state("a"), "normal")
        # Unknown souls also read as normal.
        self.assertEqual(consumer.soul_state("ghost"), "normal")

    def test_state_only_delta_flips_state_without_moving_track(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(make_snapshot([{"soul_id": "a", "x": 1.0, "y": 2.0}]))
        before = consumer.rendered_positions(now=0.0)["a"]
        consumer.apply_frame(make_delta([state_op("a", "collapsed")]))
        self.assertEqual(consumer.soul_state("a"), "collapsed")
        after = consumer.rendered_positions(now=0.0)["a"]
        self.assertEqual(before, after)

    def test_remove_clears_state(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(
            make_snapshot([{"soul_id": "a", "x": 1.0, "y": 2.0, "state": "collapsed"}])
        )
        consumer.apply_frame(
            make_delta([{"op": protocol.EntityOpKind.REMOVE.value, "soul_id": "a"}])
        )
        self.assertEqual(consumer.soul_state("a"), "normal")
        self.assertNotIn("a", consumer.soul_states())

    def test_mixed_delta_keeps_both_paths(self):
        clock = FakeClock()
        consumer = ViewportConsumer(clock=clock)
        consumer.apply_frame(make_snapshot([{"soul_id": "a", "x": 1.0, "y": 2.0}]))
        clock.advance(1.0)
        consumer.apply_frame(
            make_delta(
                [upsert_op("a", 5.0, 6.0, snap=True), state_op("a", "collapsed")]
            )
        )
        self.assertEqual(consumer.soul_state("a"), "collapsed")
        x, y = consumer.rendered_positions(now=clock())["a"]
        self.assertAlmostEqual(x, 5.0)
        self.assertAlmostEqual(y, 6.0)


class TestIsStatue(unittest.TestCase):
    def test_collapsed_is_statue(self):
        self.assertTrue(is_statue("collapsed"))

    def test_normal_and_traveling_are_not(self):
        self.assertFalse(is_statue("normal"))
        self.assertFalse(is_statue("traveling"))

    def test_none_defaults_normal(self):
        self.assertFalse(is_statue(None))

    def test_statue_states_constant(self):
        self.assertEqual(STATUE_STATE, "collapsed")


class TestStatueColors(unittest.TestCase):
    def test_statue_color_is_gray_and_dimmed(self):
        normal = (0.9, 0.2, 0.3)
        stone = statue_orb_color(normal)
        # Desaturated: channels equal...
        self.assertAlmostEqual(stone[0], stone[1])
        self.assertAlmostEqual(stone[1], stone[2])
        # ...and dimmed below the original luminance.
        lum = 0.299 * normal[0] + 0.587 * normal[1] + 0.114 * normal[2]
        self.assertLess(stone[0], lum)

    def test_statue_color_differs_from_normal(self):
        normal = (0.1, 0.8, 0.5)
        self.assertNotEqual(statue_orb_color(normal), normal)


class TestSoulStatueRendering(unittest.TestCase):
    def make_soul(self):
        return Soul.from_dict(
            {
                "soul_id": "s1",
                "name": "S",
                "orb_color": [0.9, 0.2, 0.3],
                "aura_color": [0.1, 0.8, 0.5],
            },
        )

    def test_normal_soul_uses_live_colors(self):
        soul = self.make_soul()
        self.assertFalse(soul.statue)
        self.assertEqual(soul.display_orb_color(), soul.orb_color_rgb)
        self.assertEqual(soul.display_aura_color(), soul.aura_color_rgb)

    def test_statue_soul_uses_stone_colors(self):
        soul = self.make_soul()
        soul.statue = True
        self.assertEqual(soul.display_orb_color(), statue_orb_color((0.9, 0.2, 0.3)))
        self.assertEqual(soul.display_aura_color(), statue_orb_color((0.1, 0.8, 0.5)))
        self.assertNotEqual(soul.display_orb_color(), soul.orb_color_rgb)


def dormancy_op(soul_id, dormant):
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "domain": "dormancy",
        "state": {"soul_id": soul_id, "dormant": dormant},
    }


class TestDormantConsumerTracking(unittest.TestCase):
    """Issue #22: the consumer tracks wallet-derived dormancy from the
    snapshot and the dormancy delta domain, orthogonal to the state."""

    def test_snapshot_records_dormancy_per_soul(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(
            make_snapshot(
                [
                    {"soul_id": "a", "x": 1.0, "y": 2.0, "dormant": True},
                    {"soul_id": "b", "x": 3.0, "y": 4.0, "dormant": False},
                ]
            )
        )
        self.assertTrue(consumer.is_dormant("a"))
        self.assertFalse(consumer.is_dormant("b"))

    def test_missing_dormancy_defaults_awake(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(
            make_snapshot([{"soul_id": "a", "x": 1.0, "y": 2.0}])
        )
        self.assertFalse(consumer.is_dormant("a"))
        self.assertFalse(consumer.is_dormant("ghost"))

    def test_dormancy_delta_flips_without_touching_state(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(
            make_snapshot(
                [{"soul_id": "a", "x": 1.0, "y": 2.0,
                  "state": "normal", "dormant": False}]
            )
        )
        consumer.apply_frame(make_delta([dormancy_op("a", True)]))
        self.assertTrue(consumer.is_dormant("a"))
        self.assertEqual(consumer.soul_state("a"), "normal")
        consumer.apply_frame(make_delta([dormancy_op("a", False)]))
        self.assertFalse(consumer.is_dormant("a"))

    def test_remove_clears_dormancy(self):
        consumer = ViewportConsumer(clock=lambda: 0.0)
        consumer.apply_frame(
            make_snapshot([{"soul_id": "a", "x": 1.0, "y": 2.0, "dormant": True}])
        )
        consumer.apply_frame(
            make_delta([{"op": protocol.EntityOpKind.REMOVE.value, "soul_id": "a"}])
        )
        self.assertFalse(consumer.is_dormant("a"))


if __name__ == "__main__":
    unittest.main()
