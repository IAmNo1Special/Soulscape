"""Tests for the local TOML bubble config (issue #30)."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from client.system.bubble_config import (
    BubbleConfig,
    load_bubble_config,
    save_bubble_config,
)
from client.system.noise import NoiseSettings


class TestBubbleConfig(unittest.TestCase):
    def test_missing_file_yields_defaults(self):
        config = load_bubble_config(path=pathlib.Path("/nonexistent/x.toml"))
        self.assertEqual(config.noise.caps_per_hour, 4)
        self.assertEqual(config.noise.quiet_start, "22:00")
        self.assertEqual(config.noise.quiet_end, "07:00")
        self.assertEqual(config.noise.mutes, frozenset())
        self.assertFalse(config.noise.work_mode)
        self.assertEqual(config.display.durations["quip"], 8.0)
        self.assertEqual(config.display.max_visible_per_soul, 1)
        self.assertEqual(config.exclusions, {})

    def test_corrupt_file_fails_soft(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "b.toml"
            p.write_text("this is [not valid toml", encoding="utf-8")
            config = load_bubble_config(path=p)
            self.assertEqual(config.noise.caps_per_hour, 4)

    def test_round_trip_preserves_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "b.toml"
            config = BubbleConfig(
                noise=NoiseSettings(
                    caps_per_hour=2,
                    quiet_start="23:00",
                    quiet_end="06:30",
                    mutes=frozenset({"soul-1"}),
                    work_mode=True,
                )
            )
            config.display.durations["speech"] = 9.5
            config.exclusions = {"soul-9": "no-bubbles-ever"}
            save_bubble_config(config, path=p)
            loaded = load_bubble_config(path=p)
            self.assertEqual(loaded.noise.caps_per_hour, 2)
            self.assertEqual(loaded.noise.quiet_start, "23:00")
            self.assertEqual(loaded.noise.quiet_end, "06:30")
            self.assertEqual(loaded.noise.mutes, frozenset({"soul-1"}))
            self.assertTrue(loaded.noise.work_mode)
            self.assertEqual(loaded.display.durations["speech"], 9.5)
            self.assertEqual(
                loaded.exclusions, {"soul-9": "no-bubbles-ever"}
            )

    def test_bad_values_fail_soft_per_field(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "b.toml"
            p.write_text(
                "[noise]\ncaps_per_hour = -5\nquiet_start = \"99:99\"\n"
                'mutes = "not-a-list"\n[display]\ndurations = { speech = -1.0 }\n',
                encoding="utf-8",
            )
            config = load_bubble_config(path=p)
            self.assertEqual(config.noise.caps_per_hour, 4)
            self.assertEqual(config.noise.quiet_start, "22:00")
            self.assertEqual(config.noise.mutes, frozenset())
            self.assertEqual(config.display.durations["speech"], 6.0)


if __name__ == "__main__":
    unittest.main()
