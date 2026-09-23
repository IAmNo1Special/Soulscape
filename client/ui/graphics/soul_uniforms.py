"""State -> shader uniform mapping for Soul visuals.

Pure module: no GL calls, no I/O, no clock reads. ``state_to_uniforms``
maps authoritative Soul state (biology, lifecycle) to the uniform set
the orb/aura shaders consume.

Uniform set (exact dict keys returned; the renderer uploads them):
    base_color_uniform: (r, g, b) floats -- display color after the
        biology tint and desaturation steps below.
    desat_factor: float 0..1 -- 0 full color, 1 full grayscale.
    brightness: float -- biology brightness multiplier, 1.0 at
        full vitality (neutral: the authored look is preserved). The orb
        pass uploads it as ``brightness``; the aura pass uploads
        ``brightness * 0.8`` as its existing ``base_brightness`` uniform
        (0.8 is the legacy AURA_BASE_BRIGHTNESS).
    opacity: float 0..1 -- fade alpha multiplier (expedition walk-off).
        The orb pass blends with SRC_ALPHA, so sub-1.0 opacity visibly dips.
    pulse_rate: float -- plasma-pulse speed multiplier. 0 freezes the
        plasma pattern (statues). Scales the ``time`` terms in the
        fragment shaders; vertex-shader noise stays ambient.
    pulse_strength: float -- plasma-pulse amplitude multiplier.
    bob_amplitude / bob_speed / bob_phase: floats -- model-space hover
        bob. The shaders take no bob uniforms, so the renderer applies
        these to the model matrix (transform motion).

Curves (documented so goldens are auditable):
    vitality = 0.40*satiety + 0.35*hydration + 0.25*hp, inputs 0..1.
    pulse_rate     = 1.0 + 1.8*(1-vitality)   (starving: fast, weak)
    pulse_strength = 0.25 + 0.75*vitality     (healthy: slow, strong)
    brightness     = 0.45 + 0.55*vitality     (healthy == 1.0, neutral;
                     starving == 0.45)

Statue variants (frozen motion: pulse -> 0; the camera orbit freezes
because ``visual_tick`` is skipped for statues so ``soul.time`` stops):
    collapsed: desat 1.0, brightness 1.0 (the stone color is pre-baked
      by Soul.display_*_color), opacity 1.0, no bob.

All floats are rounded to 6 decimals so golden tests compare exact
dicts; any future change to the curves breaks the goldens loudly.
"""

from __future__ import annotations

from typing import Any, Mapping

STATUE_COLLAPSED = "collapsed"

_FLOAT_PRECISION = 6


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _r(value: float) -> float:
    return round(float(value), _FLOAT_PRECISION)


def state_to_uniforms(state: Mapping[str, Any]) -> dict[str, Any]:
    """Map authoritative Soul state to shader uniforms.

    Args:
        state: mapping with keys ``satiety``, ``hydration``, ``hp``
            (0..1 fractions), ``statue_kind`` (None or one of the
            STATUE_* constants), ``base_color`` ((r, g, b)),
            ``fade_alpha`` (0..1, issue #35: expedition walk-off/walk-in).

    Returns:
        Dict with exactly the keys documented in the module docstring.
    """
    satiety = _clamp01(state.get("satiety", 1.0))
    hydration = _clamp01(state.get("hydration", 1.0))
    hp = _clamp01(state.get("hp", 1.0))
    statue_kind = state.get("statue_kind")
    fade_alpha = _clamp01(state.get("fade_alpha", 1.0))
    base = state.get("base_color", (1.0, 1.0, 1.0))
    r0, g0, b0 = float(base[0]), float(base[1]), float(base[2])

    vitality = 0.40 * satiety + 0.35 * hydration + 0.25 * hp

    pulse_rate = 1.0 + 1.8 * (1.0 - vitality)
    pulse_strength = 0.25 + 0.75 * vitality
    brightness = 0.45 + 0.55 * vitality
    desat_factor = 0.0
    opacity = fade_alpha
    bob_amplitude = 0.02
    bob_speed = 1.6
    bob_phase = 0.0

    if statue_kind == STATUE_COLLAPSED:
        desat_factor = 1.0
        brightness = 1.0
        pulse_rate = 0.0
        pulse_strength = 0.0
        opacity = 1.0
        bob_amplitude = 0.0
        bob_speed = 0.0

    return {
        "base_color_uniform": (_r(r0), _r(g0), _r(b0)),
        "desat_factor": _r(desat_factor),
        "brightness": _r(brightness),
        "opacity": _r(opacity),
        "pulse_rate": _r(pulse_rate),
        "pulse_strength": _r(pulse_strength),
        "bob_amplitude": _r(bob_amplitude),
        "bob_speed": _r(bob_speed),
        "bob_phase": _r(bob_phase),
    }