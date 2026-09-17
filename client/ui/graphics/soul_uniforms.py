"""State -> shader uniform mapping for Soul visuals (issue #29).

Pure module: no GL calls, no I/O, no clock reads. ``state_to_uniforms``
maps authoritative Soul state (biology, lifecycle, presence, local
visual reflexes) to the uniform set the orb/aura shaders consume, so
pet visuals express state through shader uniforms and transform motion
instead of ad-hoc per-soul values in the renderer.

Uniform set (exact dict keys returned; the renderer uploads them):
    base_color_uniform: (r, g, b) floats -- display color after the
        biology tint and desaturation steps below.
    desat_factor: float 0..1 -- 0 full color, 1 full grayscale.
    brightness: float -- biology/reflex brightness multiplier, 1.0 at
        full vitality (neutral: the authored look is preserved). The orb
        pass uploads it as ``brightness``; the aura pass uploads
        ``brightness * 0.8`` as its existing ``base_brightness`` uniform
        (0.8 is the legacy AURA_BASE_BRIGHTNESS).
    opacity: float 0..1 -- typing-dip / reflex alpha multiplier. The
        orb pass blends with SRC_ALPHA, so sub-1.0 opacity visibly dips.
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
    tint: the largest deficit >= 0.15 shifts the base color toward a
      warning hue at 0.35*deficit strength. Hungry -> warm
      (1.0, 0.55, 0.25); thirsty -> cool (0.35, 0.65, 1.0); wounded ->
      pale (1.0, 0.5, 0.5). Ties break satiety > hydration > hp.

Statue variants (frozen motion: pulse -> 0; the camera orbit freezes
because ``visual_tick`` is skipped for statues so ``soul.time`` stops):
    collapsed: desat 1.0, brightness 1.0 (the stone color is pre-baked
      by Soul.display_*_color, issue #21), opacity 1.0, no bob.
    dormant:   desat 0.0 so the amber stone tint (issue #22) survives,
      brightness 1.0, opacity 1.0, no bob.
    offline:   desat 0.85, brightness 0.6, opacity 1.0, no bob.
"Offline" reading: a soul whose Hub presence is stale/unknown -- its
viewport track has no fresh samples (the consumer staleness check).
NOT the client's offline mode: offline-mode souls run the local sim
and render ACTIVE.

Reflex overlays (water-cooler hooks, client-side; applied last and
only for non-statue souls -- statues stay frozen):
    greeting (t in [0, 2.0)): brightness swell + bob on unlock.
    nap: slow dim pulse while the tamer is long-idle.
    reaction (t in [0, 1.2)): pulse spike on an input-activity burst.
    typing_dip (0..1): opacity = 1 - 0.45*dip; the dip itself is
      computed locally from the coarse input-activity signal and is
      never transmitted or logged (see visual_reflexes.py).

All floats are rounded to 6 decimals so golden tests compare exact
dicts; any future change to the curves breaks the goldens loudly.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

STATUE_COLLAPSED = "collapsed"
STATUE_DORMANT = "dormant"
STATUE_OFFLINE = "offline"

REFLEX_GREETING = "greeting"
REFLEX_NAP = "nap"
REFLEX_REACTION = "reaction"

GREETING_DURATION_S = 2.0
REACTION_DURATION_S = 1.2
TYPING_DIP_DEPTH = 0.45

_FLOAT_PRECISION = 6


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _r(value: float) -> float:
    return round(float(value), _FLOAT_PRECISION)


def _tint_for_deficit(
    satiety: float, hydration: float, hp: float
) -> tuple[float, float, float] | None:
    deficits = (
        (1.0 - satiety, (1.0, 0.55, 0.25)),
        (1.0 - hydration, (0.35, 0.65, 1.0)),
        (1.0 - hp, (1.0, 0.5, 0.5)),
    )
    worst = max(deficits, key=lambda item: item[0])
    magnitude, color = worst
    if magnitude < 0.15:
        return None
    strength = 0.35 * magnitude
    return (color, strength)


def state_to_uniforms(state: Mapping[str, Any]) -> dict[str, Any]:
    """Map authoritative Soul state to shader uniforms.

    Args:
        state: mapping with keys ``satiety``, ``hydration``, ``hp``
            (0..1 fractions), ``statue_kind`` (None or one of the
            STATUE_* constants), ``typing_dip`` (0..1), ``reflex``
            (None or one of the REFLEX_* constants), ``reflex_t``
            (seconds into the reflex), ``base_color`` ((r, g, b)).

    Returns:
        Dict with exactly the keys documented in the module docstring.
    """
    satiety = _clamp01(state.get("satiety", 1.0))
    hydration = _clamp01(state.get("hydration", 1.0))
    hp = _clamp01(state.get("hp", 1.0))
    statue_kind = state.get("statue_kind")
    typing_dip = _clamp01(state.get("typing_dip", 0.0))
    reflex = state.get("reflex")
    reflex_t = max(0.0, float(state.get("reflex_t", 0.0)))
    base = state.get("base_color", (1.0, 1.0, 1.0))
    r0, g0, b0 = float(base[0]), float(base[1]), float(base[2])

    vitality = 0.40 * satiety + 0.35 * hydration + 0.25 * hp

    pulse_rate = 1.0 + 1.8 * (1.0 - vitality)
    pulse_strength = 0.25 + 0.75 * vitality
    brightness = 0.45 + 0.55 * vitality
    desat_factor = 0.0
    opacity = 1.0 - TYPING_DIP_DEPTH * typing_dip
    bob_amplitude = 0.02
    bob_speed = 1.6
    bob_phase = 0.0

    tint = _tint_for_deficit(satiety, hydration, hp)
    if tint is not None:
        color, strength = tint
        r0 = r0 + (color[0] - r0) * strength
        g0 = g0 + (color[1] - g0) * strength
        b0 = b0 + (color[2] - b0) * strength

    if statue_kind == STATUE_COLLAPSED:
        desat_factor = 1.0
        brightness = 1.0
        pulse_rate = 0.0
        pulse_strength = 0.0
        opacity = 1.0
        bob_amplitude = 0.0
        bob_speed = 0.0
    elif statue_kind == STATUE_DORMANT:
        desat_factor = 0.0
        brightness = 1.0
        pulse_rate = 0.0
        pulse_strength = 0.0
        opacity = 1.0
        bob_amplitude = 0.0
        bob_speed = 0.0
    elif statue_kind == STATUE_OFFLINE:
        desat_factor = 0.85
        brightness = 0.6
        pulse_rate = 0.0
        pulse_strength = 0.0
        opacity = 1.0
        bob_amplitude = 0.0
        bob_speed = 0.0
    elif reflex == REFLEX_GREETING and reflex_t < GREETING_DURATION_S:
        swell = math.sin(math.pi * reflex_t / GREETING_DURATION_S)
        brightness *= 1.0 + 0.35 * swell
        pulse_rate *= 1.0 + 0.5 * swell
        bob_amplitude = max(bob_amplitude, 0.06 * swell)
    elif reflex == REFLEX_NAP:
        pulse_rate = 0.35
        pulse_strength *= 0.5
        brightness *= 0.55
        bob_amplitude = 0.01
        bob_speed = 0.5
    elif reflex == REFLEX_REACTION and reflex_t < REACTION_DURATION_S:
        spike = math.exp(-3.0 * reflex_t)
        pulse_rate *= 1.0 + 1.2 * spike
        pulse_strength = min(1.3, pulse_strength * (1.0 + 0.3 * spike))
        brightness *= 1.0 + 0.15 * spike
        bob_amplitude += 0.03 * spike

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
