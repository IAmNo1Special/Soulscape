"""Tests for the bubble manager seam (issue #30).

Headless: BubbleManager is pyglet-free; layout() returns draw jobs and
the scene renderer consumes them.
"""

from __future__ import annotations

import unittest

from client.system.noise import NoisePolicy, NoiseSettings
from client.ui.bubbles import (
    BUBBLE_KINDS,
    BUBBLE_Y_OFFSET,
    BubbleManager,
)


class FakeClock:
    def __init__(self, start: float = 5000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _manager(clock=None, **kw) -> BubbleManager:
    clock = clock or FakeClock()
    policy = NoisePolicy(NoiseSettings(), clock=clock)
    return BubbleManager(policy=policy, clock=clock, **kw)


class TestBubbleKinds(unittest.TestCase):
    def test_closed_kind_set(self):
        self.assertEqual(
            BUBBLE_KINDS, {"speech", "quip", "system", "greeting"}
        )

    def test_unknown_kind_raises(self):
        mgr = _manager()
        with self.assertRaises(ValueError):
            mgr.show_bubble("s1", "hi", kind="shout")

    def test_empty_text_rejected(self):
        mgr = _manager()
        result = mgr.show_bubble("s1", "   ")
        self.assertFalse(result.accepted)


class TestLifecycle(unittest.TestCase):
    def test_auto_dismiss_per_kind(self):
        clock = FakeClock()
        mgr = _manager(clock)
        mgr.show_bubble("s1", "speech!", kind="speech")
        mgr.show_bubble("s2", "quip!", kind="quip")
        clock.advance(6.5)
        expired = mgr.tick()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].kind, "speech")
        self.assertEqual(len(mgr.visible_bubbles("s1")), 0)
        self.assertEqual(len(mgr.visible_bubbles("s2")), 1)
        clock.advance(2.0)
        mgr.tick()
        self.assertEqual(len(mgr.visible_bubbles("s2")), 0)

    def test_max_visible_one_queues_rest(self):
        mgr = _manager()
        mgr.show_bubble("s1", "first")
        mgr.show_bubble("s1", "second")
        self.assertEqual(len(mgr.visible_bubbles("s1")), 1)
        self.assertEqual(mgr.visible_bubbles("s1")[0].text, "first")
        self.assertEqual(mgr.queued_count("s1"), 1)

    def test_queue_promotes_on_expiry(self):
        clock = FakeClock()
        mgr = _manager(clock)
        mgr.show_bubble("s1", "first")
        mgr.show_bubble("s1", "second")
        clock.advance(7.0)
        mgr.tick()
        visible = mgr.visible_bubbles("s1")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].text, "second")

    def test_queue_overflow_drops_oldest(self):
        mgr = _manager(queue_depth=2)
        mgr.show_bubble("s1", "visible")
        mgr.show_bubble("s1", "q1")
        mgr.show_bubble("s1", "q2")
        mgr.show_bubble("s1", "q3")
        self.assertEqual(mgr.queued_count("s1"), 2)

    def test_layout_offsets_above_orb(self):
        mgr = _manager()
        mgr.show_bubble("s1", "hello")
        jobs = mgr.layout({"s1": (100.0, 200.0)})
        self.assertEqual(len(jobs), 1)
        x, y, text = jobs[0]
        self.assertEqual(x, 100.0)
        self.assertEqual(y, 200.0 + BUBBLE_Y_OFFSET)
        self.assertEqual(text, "hello")

    def test_layout_skips_unknown_positions(self):
        mgr = _manager()
        mgr.show_bubble("s1", "hello")
        self.assertEqual(mgr.layout({}), [])


class TestNoiseIntegration(unittest.TestCase):
    def test_cap_suppresses_fifth_bubble(self):
        clock = FakeClock()
        policy = NoisePolicy(NoiseSettings(caps_per_hour=4), clock=clock)
        mgr = BubbleManager(policy=policy, clock=clock)
        for i in range(4):
            result = mgr.show_bubble("s1", f"msg {i}")
            self.assertTrue(result.accepted)
            clock.advance(1.0)
        result = mgr.show_bubble("s1", "msg 4")
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "cap")

    def test_solicited_quip_bypasses_cap(self):
        clock = FakeClock()
        policy = NoisePolicy(NoiseSettings(caps_per_hour=1), clock=clock)
        mgr = BubbleManager(policy=policy, clock=clock)
        mgr.show_bubble("s1", "ambient")
        result = mgr.show_bubble("s1", "quip!", kind="quip", solicited=True)
        self.assertTrue(result.accepted)


if __name__ == "__main__":
    unittest.main()
