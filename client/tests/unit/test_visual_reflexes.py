"""Unit tests for the issue #29 water-cooler reflex controller.

Fake clock + scriptable sampler throughout. Covers the greeting
latency budget, nap engagement/clearing, burst reaction + typing-dip
decay, and the local-only privacy posture (the detector needs nothing
beyond the narrow PresenceSampler interface, and nothing it computes
is shaped for transmission).
"""

from __future__ import annotations

import unittest

from client.system.presence import PresenceSampler
from client.ui.graphics.soul_uniforms import (
    GREETING_DURATION_S,
    REACTION_DURATION_S,
)
from client.ui.graphics.visual_reflexes import (
    GREETING_LATENCY_BUDGET_S,
    TYPING_DIP_DECAY_S,
    TypingBurstDetector,
    VisualReflexController,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class ScriptedSampler(PresenceSampler):
    def __init__(self, age_s: float = 0.0, locked: bool = False) -> None:
        self.age_s = age_s
        self.locked = locked

    def last_input_age_s(self) -> float:
        return self.age_s

    def is_locked(self) -> bool:
        return self.locked

    def foreground_category(self):
        return None


class AgeOnlySampler:
    """Implements ONLY last_input_age_s: proves the burst detector
    needs no other raw signal."""

    def __init__(self) -> None:
        self.age_s = 0.0

    def last_input_age_s(self) -> float:
        return self.age_s


class TestTypingBurstDetector(unittest.TestCase):
    def make(self, clock):
        return TypingBurstDetector(AgeOnlySampler(), clock=clock)

    def test_needs_only_last_input_age(self):
        clock = FakeClock()
        detector = self.make(clock)
        self.assertFalse(detector.poll(clock()))
        self.assertEqual(detector.dip(clock()), 0.0)

    def test_burst_after_three_edges_in_window(self):
        clock = FakeClock()
        sampler = AgeOnlySampler()
        detector = TypingBurstDetector(sampler, clock=clock)
        sampler.age_s = 5.0
        self.assertFalse(detector.poll(clock()))
        for _ in range(3):
            clock.advance(0.3)
            sampler.age_s = 0.01
            burst = detector.poll(clock())
            sampler.age_s = 5.0
            detector.poll(clock())
        self.assertTrue(burst)

    def test_idle_never_bursts(self):
        clock = FakeClock()
        sampler = AgeOnlySampler()
        detector = TypingBurstDetector(sampler, clock=clock)
        for _ in range(20):
            clock.advance(0.25)
            sampler.age_s += 0.25
            self.assertFalse(detector.poll(clock()))

    def test_dip_decays_after_burst(self):
        clock = FakeClock()
        sampler = AgeOnlySampler()
        detector = TypingBurstDetector(sampler, clock=clock)
        sampler.age_s = 5.0
        detector.poll(clock())
        for _ in range(3):
            clock.advance(0.2)
            sampler.age_s = 0.01
            detector.poll(clock())
            sampler.age_s = 5.0
            detector.poll(clock())
        burst_at = clock()
        self.assertAlmostEqual(detector.dip(burst_at), 1.0)
        self.assertAlmostEqual(detector.dip(burst_at + TYPING_DIP_DECAY_S / 2), 0.5)
        self.assertEqual(detector.dip(burst_at + TYPING_DIP_DECAY_S), 0.0)
        self.assertEqual(detector.dip(burst_at + 10.0), 0.0)


class TestVisualReflexController(unittest.TestCase):
    def make(self, **sampler_kw):
        clock = FakeClock()
        sampler = ScriptedSampler(**sampler_kw)
        return VisualReflexController(sampler=sampler, clock=clock), clock

    def test_unlock_triggers_greeting_within_latency_budget(self):
        controller, clock = self.make(locked=True)
        controller.frame(["s1"], clock())
        clock.advance(0.1)
        controller._redactor._sampler.locked = False
        overlays = controller.frame(["s1"], clock())
        overlay = overlays["s1"]
        self.assertEqual(overlay.reflex, "greeting")
        self.assertLessEqual(overlay.reflex_t, GREETING_LATENCY_BUDGET_S)

    def test_greeting_expires_after_duration(self):
        controller, clock = self.make(locked=True)
        controller.frame(["s1"], clock())
        controller._redactor._sampler.locked = False
        controller.frame(["s1"], clock())
        clock.advance(GREETING_DURATION_S + 0.5)
        overlays = controller.frame(["s1"], clock())
        self.assertIsNone(overlays["s1"].reflex)

    def test_long_idle_triggers_nap_and_activity_clears_it(self):
        controller, clock = self.make(age_s=2000.0)
        overlays = controller.frame(["s1", "s2"], clock())
        self.assertEqual(overlays["s1"].reflex, "nap")
        self.assertEqual(overlays["s2"].reflex, "nap")
        controller._redactor._sampler.age_s = 1.0
        clock.advance(1.0)
        overlays = controller.frame(["s1"], clock())
        self.assertIsNone(overlays["s1"].reflex)

    def test_tamer_return_triggers_greeting(self):
        controller, clock = self.make(age_s=2000.0)
        controller.frame(["s1"], clock())
        clock.advance(31 * 60.0)
        controller._redactor._sampler.age_s = 0.0
        overlays = controller.frame(["s1"], clock())
        self.assertEqual(overlays["s1"].reflex, "greeting")

    def test_input_burst_triggers_reaction_and_dip(self):
        controller, clock = self.make(age_s=5.0)
        sampler = controller._redactor._sampler
        controller.frame(["s1"], clock())
        burst_seen = False
        for _ in range(4):
            clock.advance(0.3)
            sampler.age_s = 0.01
            overlays = controller.frame(["s1"], clock())
            sampler.age_s = 5.0
            controller.frame(["s1"], clock())
            if overlays["s1"].reflex == "reaction":
                burst_seen = True
                break
        self.assertTrue(burst_seen)
        self.assertGreater(overlays["s1"].typing_dip, 0.0)
        clock.advance(REACTION_DURATION_S + 0.5)
        overlays = controller.frame(["s1"], clock())
        self.assertIsNone(overlays["s1"].reflex)

    def test_overlay_carries_no_counts_or_content(self):
        controller, clock = self.make()
        overlays = controller.frame(["s1"], clock())
        overlay = overlays["s1"]
        self.assertEqual(sorted(overlay.__dict__), ["reflex", "reflex_t", "typing_dip"])
        self.assertIsInstance(overlay.typing_dip, float)

    def test_controller_has_no_network_or_log_surface(self):
        controller, _ = self.make()
        for attr in ("send", "ship", "log", "save", "persist"):
            self.assertFalse(hasattr(controller, attr), f"unexpected surface: {attr}")


if __name__ == "__main__":
    unittest.main()
