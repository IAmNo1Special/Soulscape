"""Simulated-clock tests for the noise policy (issue #30).

Every decision is a pure function of (config, clock, history): the
FakeClock stands in for time and a fake localtime stands in for the
tamer's timezone.
"""

from __future__ import annotations

import time
import unittest

from client.system.noise import (
    REASON_CAP,
    REASON_MUTED,
    REASON_OK,
    REASON_QUIET_HOURS,
    REASON_WORK_MODE,
    NoisePolicy,
    NoiseSettings,
    decide,
    in_quiet_hours,
)


def _localtime_at(hour: int, minute: int = 0):
    def fake(now: float) -> time.struct_time:
        return time.struct_time((2026, 9, 17, hour, minute, 0, 3, 260, -1))

    return fake


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _settings(**kw) -> NoiseSettings:
    base = {
        "caps_per_hour": 4,
        "quiet_start": "22:00",
        "quiet_end": "07:00",
        "mutes": frozenset(),
        "work_mode": False,
    }
    base.update(kw)
    return NoiseSettings(**base)


class TestQuietHours(unittest.TestCase):
    def test_inside_window_crossing_midnight(self):
        lt = _localtime_at(23, 30)
        self.assertTrue(in_quiet_hours(0.0, "22:00", "07:00", lt))
        self.assertTrue(in_quiet_hours(0.0, "22:00", "07:00", _localtime_at(3)))

    def test_outside_window(self):
        self.assertFalse(in_quiet_hours(0.0, "22:00", "07:00", _localtime_at(12)))
        self.assertFalse(in_quiet_hours(0.0, "22:00", "07:00", _localtime_at(8)))

    def test_window_edges(self):
        self.assertTrue(in_quiet_hours(0.0, "22:00", "07:00", _localtime_at(22)))
        self.assertFalse(in_quiet_hours(0.0, "22:00", "07:00", _localtime_at(7)))

    def test_daytime_window(self):
        self.assertTrue(in_quiet_hours(0.0, "13:00", "14:00", _localtime_at(13, 30)))
        self.assertFalse(in_quiet_hours(0.0, "13:00", "14:00", _localtime_at(15)))

    def test_bad_bounds_fail_open(self):
        self.assertFalse(in_quiet_hours(0.0, "nope", "07:00", _localtime_at(23)))


class TestDecide(unittest.TestCase):
    def test_fifth_unsolicited_within_hour_suppressed(self):
        clock = FakeClock()
        policy = NoisePolicy(_settings(), clock=clock,
                             localtime=_localtime_at(12))
        for _ in range(4):
            self.assertEqual(policy.decide("s1"), REASON_OK)
            policy.note_shown("s1")
        self.assertEqual(policy.decide("s1"), REASON_CAP)

    def test_cap_is_per_soul(self):
        clock = FakeClock()
        policy = NoisePolicy(_settings(), clock=clock,
                             localtime=_localtime_at(12))
        for _ in range(4):
            policy.note_shown("s1")
        self.assertEqual(policy.decide("s2"), REASON_OK)

    def test_cap_rolls_off_after_an_hour(self):
        clock = FakeClock()
        policy = NoisePolicy(_settings(), clock=clock,
                             localtime=_localtime_at(12))
        for _ in range(4):
            policy.note_shown("s1")
        self.assertEqual(policy.decide("s1"), REASON_CAP)
        clock.advance(3600.0)
        self.assertEqual(policy.decide("s1"), REASON_OK)

    def test_solicited_bypasses_cap(self):
        clock = FakeClock()
        policy = NoisePolicy(_settings(), clock=clock,
                             localtime=_localtime_at(12))
        for _ in range(4):
            policy.note_shown("s1")
        self.assertEqual(
            policy.decide("s1", solicited=True), REASON_OK
        )

    def test_solicited_does_not_consume_budget(self):
        clock = FakeClock()
        policy = NoisePolicy(_settings(), clock=clock,
                             localtime=_localtime_at(12))
        for _ in range(4):
            policy.note_shown("s1", solicited=True)
        self.assertEqual(policy.decide("s1"), REASON_OK)

    def test_quiet_hours_suppress_unsolicited(self):
        policy = NoisePolicy(
            _settings(), clock=FakeClock(), localtime=_localtime_at(23)
        )
        self.assertEqual(policy.decide("s1"), REASON_QUIET_HOURS)

    def test_solicited_bypasses_quiet_hours(self):
        policy = NoisePolicy(
            _settings(), clock=FakeClock(), localtime=_localtime_at(23)
        )
        self.assertEqual(policy.decide("s1", solicited=True), REASON_OK)

    def test_mute_suppresses_everything(self):
        policy = NoisePolicy(
            _settings(mutes=frozenset({"s1"})),
            clock=FakeClock(),
            localtime=_localtime_at(12),
        )
        self.assertEqual(policy.decide("s1"), REASON_MUTED)
        self.assertEqual(policy.decide("s1", solicited=True), REASON_MUTED)

    def test_work_mode_suppresses_unsolicited_only(self):
        policy = NoisePolicy(
            _settings(work_mode=True),
            clock=FakeClock(),
            localtime=_localtime_at(12),
        )
        self.assertEqual(policy.decide("s1"), REASON_WORK_MODE)
        self.assertEqual(policy.decide("s1", solicited=True), REASON_OK)

    def test_pure_decide_function(self):
        settings = _settings()
        now = 1_000_000.0
        lt = _localtime_at(12)
        self.assertEqual(
            decide(settings, "s1", False, [now - 10] * 4, now, lt), REASON_CAP
        )
        self.assertEqual(
            decide(settings, "s1", True, [now - 10] * 4, now, lt), REASON_OK
        )

    def test_zero_cap_disables_unsolicited(self):
        policy = NoisePolicy(
            _settings(caps_per_hour=0),
            clock=FakeClock(),
            localtime=_localtime_at(12),
        )
        self.assertEqual(policy.decide("s1"), REASON_CAP)
        self.assertEqual(policy.decide("s1", solicited=True), REASON_OK)


if __name__ == "__main__":
    unittest.main()
