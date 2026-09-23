"""Golden tests for the issue #29 shader-state mapping.

state_to_uniforms is pure, so every fixture below pins the EXACT
uniform dict. If any curve changes, these fail loudly -- that is the
point. Expected values were hand-derived from the formulas documented
in client/ui/graphics/soul_uniforms.py.
"""

from __future__ import annotations

import unittest

from client.ui.graphics.soul_uniforms import (
    state_to_uniforms,
)


def make_state(**overrides):
    state = {
        "satiety": 1.0,
        "hydration": 1.0,
        "hp": 1.0,
        "statue_kind": None,
        "base_color": (0.9, 0.2, 0.3),
    }
    state.update(overrides)
    return state


class TestUniformKeys(unittest.TestCase):
    def test_exact_key_set(self):
        uniforms = state_to_uniforms(make_state())
        self.assertEqual(
            sorted(uniforms),
            sorted(
                [
                    "base_color_uniform",
                    "desat_factor",
                    "brightness",
                    "opacity",
                    "pulse_rate",
                    "pulse_strength",
                    "bob_amplitude",
                    "bob_speed",
                    "bob_phase",
                ]
            ),
        )


class TestGoldenUniforms(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(
            state_to_uniforms(make_state()),
            {
                "base_color_uniform": (0.9, 0.2, 0.3),
                "desat_factor": 0.0,
                "brightness": 1.0,
                "opacity": 1.0,
                "pulse_rate": 1.0,
                "pulse_strength": 1.0,
                "bob_amplitude": 0.02,
                "bob_speed": 1.6,
                "bob_phase": 0.0,
            },
        )

    def test_hungry(self):
        self.assertEqual(
            state_to_uniforms(make_state(satiety=0.4)),
            {
                "base_color_uniform": (0.9, 0.2, 0.3),
                "desat_factor": 0.0,
                "brightness": 0.868,
                "opacity": 1.0,
                "pulse_rate": 1.432,
                "pulse_strength": 0.82,
                "bob_amplitude": 0.02,
                "bob_speed": 1.6,
                "bob_phase": 0.0,
            },
        )

    def test_starving(self):
        self.assertEqual(
            state_to_uniforms(make_state(satiety=0.05, hydration=0.1, hp=0.3)),
            {
                "base_color_uniform": (0.9, 0.2, 0.3),
                "desat_factor": 0.0,
                "brightness": 0.5215,
                "opacity": 1.0,
                "pulse_rate": 2.566,
                "pulse_strength": 0.3475,
                "bob_amplitude": 0.02,
                "bob_speed": 1.6,
                "bob_phase": 0.0,
            },
        )

    def test_thirsty(self):
        self.assertEqual(
            state_to_uniforms(make_state(hydration=0.3)),
            {
                "base_color_uniform": (0.9, 0.2, 0.3),
                "desat_factor": 0.0,
                "brightness": 0.86525,
                "opacity": 1.0,
                "pulse_rate": 1.441,
                "pulse_strength": 0.81625,
                "bob_amplitude": 0.02,
                "bob_speed": 1.6,
                "bob_phase": 0.0,
            },
        )

    def test_low_hp(self):
        self.assertEqual(
            state_to_uniforms(make_state(hp=0.2)),
            {
                "base_color_uniform": (0.9, 0.2, 0.3),
                "desat_factor": 0.0,
                "brightness": 0.89,
                "opacity": 1.0,
                "pulse_rate": 1.36,
                "pulse_strength": 0.85,
                "bob_amplitude": 0.02,
                "bob_speed": 1.6,
                "bob_phase": 0.0,
            },
        )

    def test_collapsed_statue(self):
        self.assertEqual(
            state_to_uniforms(make_state(statue_kind="collapsed")),
            {
                "base_color_uniform": (0.9, 0.2, 0.3),
                "desat_factor": 1.0,
                "brightness": 1.0,
                "opacity": 1.0,
                "pulse_rate": 0.0,
                "pulse_strength": 0.0,
                "bob_amplitude": 0.0,
                "bob_speed": 0.0,
                "bob_phase": 0.0,
            },
        )


class TestMappingRules(unittest.TestCase):
    def test_statue_ignores_biology(self):
        starving = make_state(
            satiety=0.0,
            hydration=0.0,
            hp=0.0,
        )
        uniforms = state_to_uniforms(
            make_state(**{**starving, "statue_kind": "collapsed"})
        )
        self.assertEqual(uniforms["pulse_rate"], 0.0)
        self.assertEqual(uniforms["pulse_strength"], 0.0)
        self.assertEqual(uniforms["bob_amplitude"], 0.0)
        self.assertEqual(uniforms["opacity"], 1.0)

    def test_inputs_clamped(self):
        clamped = state_to_uniforms(
            make_state(satiety=9.0, hydration=-3.0, hp=99.0)
        )
        manual = state_to_uniforms(make_state(satiety=1.0, hydration=0.0, hp=1.0))
        self.assertEqual(clamped, manual)

    def test_collapsed_fully_desaturated(self):
        statue = state_to_uniforms(make_state(statue_kind="collapsed"))
        self.assertEqual(statue["desat_factor"], 1.0)
        self.assertEqual(statue["pulse_rate"], 0.0)
        self.assertEqual(statue["pulse_strength"], 0.0)
        self.assertEqual(statue["bob_amplitude"], 0.0)
        self.assertEqual(statue["bob_speed"], 0.0)


if __name__ == "__main__":
    unittest.main()