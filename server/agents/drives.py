"""Drive vector (issue #24): arithmetic-only, no LLM, no lookups.

Drives are computed from the Soul's nature plus live state with plain
arithmetic. They set the reflex layer's thresholds and will pre-rank
#25's deliberation options.

There is no species-profile table in v1, so the non-survival drives
come from nature baselines with documented v0 defaults. Loyalty's
learned scalar (issue #31) is read from storage when provided:
`souls.loyalty` (nudged +0.02 per petting toward 1.0, drifting toward
the nature baseline during co-presence per the arch doc). Without a
stored scalar, loyalty falls back to the nature baseline, clamped to
[0, 1].

Survival is the only drive fed by live state in v0:
    survival = 1 - min(satiety, hydration, hp_fraction * 100) / 100
so it spikes toward 1.0 when starving, dehydrated, or near collapse.
Fear is separate from the six drives: with no combat in v1 there are
no threat kinds, so fear is 0.0 by construction (documented below).
"""

#: The six drives, in canonical order.
DRIVES = ("survival", "wealth", "social", "curiosity", "loyalty", "aggression")

#: Default baseline for every drive when the nature carries no signal.
BASELINE = 0.5

#: v0 nature -> drive deltas around BASELINE. These are taste defaults,
#: not simulation truth; species profiles (#34+) will refine them.
_NATURE_DELTAS: dict[str, dict[str, float]] = {
    "brave": {"aggression": 0.15, "curiosity": 0.05},
    "bold": {"aggression": 0.1, "social": 0.05},
    "adamant": {"aggression": 0.1, "wealth": 0.05},
    "naughty": {"aggression": 0.1, "curiosity": 0.1},
    "timid": {"social": -0.1, "aggression": -0.1},
    "calm": {"aggression": -0.15, "social": 0.05},
    "gentle": {"aggression": -0.15, "loyalty": 0.1},
    "docile": {"social": -0.1, "curiosity": -0.05},
    "bashful": {"social": -0.1},
    "quiet": {"social": -0.1, "curiosity": 0.05},
    "jolly": {"social": 0.2, "curiosity": 0.1},
    "hasty": {"curiosity": 0.15, "wealth": 0.05},
    "naive": {"curiosity": 0.15, "social": 0.1},
    "rash": {"curiosity": 0.1, "aggression": 0.05},
    "lonely": {"social": 0.15, "loyalty": 0.1},
    "relaxed": {"wealth": 0.05, "aggression": -0.05},
    "lax": {"curiosity": -0.05, "aggression": -0.05},
    "careful": {"curiosity": -0.1, "wealth": 0.1},
    "sassy": {"social": 0.1, "aggression": 0.05},
    "impish": {"curiosity": 0.1, "social": 0.05},
    "mild": {"aggression": -0.05},
    "modest": {"social": -0.05},
    "hardy": {},
    "serious": {"wealth": 0.05},
    "quirky": {"curiosity": 0.05},
}

#: Entity kinds that count as threats for the fear term. Empty in v1:
#: no combat, no predators, so fear is 0.0. When threat kinds exist
#: (Stage 1+), add them here with per-kind weights.
_THREAT_WEIGHTS: dict[str, float] = {}


def nature_baseline(nature: str | None, drive: str) -> float:
    """Nature baseline for one drive, clamped to [0, 1]."""
    deltas = _NATURE_DELTAS.get((nature or "").strip().lower(), {})
    return min(1.0, max(0.0, BASELINE + deltas.get(drive, 0.0)))


def survival_drive(
    satiety: float | None,
    hydration: float | None,
    hp: float | None,
    max_hp: float | None,
) -> float:
    """Survival pressure in [0, 1]: 0 = sated, 1 = dying.

    NULL biology fields (legacy rows) read as full -- no phantom
    starvation from missing data.
    """
    sat = 100.0 if satiety is None else float(satiety)
    hyd = 100.0 if hydration is None else float(hydration)
    if hp is None or not max_hp:
        hp_frac = 1.0
    else:
        hp_frac = min(1.0, max(0.0, float(hp) / float(max_hp)))
    worst = min(sat, hyd, hp_frac * 100.0)
    return min(1.0, max(0.0, 1.0 - worst / 100.0))


def fear_from_observations(observations: list[dict]) -> float:
    """Fear in [0, 1] from the nearest weighted threat in detail vision.

    v0: _THREAT_WEIGHTS is empty (no combat, no predators), so this
    always returns 0.0. The shape is here so flee reflex logic and
    tests can exercise the threshold path with a non-empty table.
    """
    fear = 0.0
    for obs in observations:
        weight = _THREAT_WEIGHTS.get(str(obs.get("kind", "")), 0.0)
        if weight <= 0.0:
            continue
        distance = max(float(obs.get("distance", 0.0)), 1.0)
        fear = max(fear, weight / (1.0 + distance / 40.0))
    return min(1.0, fear)


def compute_drives(
    nature: str | None,
    satiety: float | None,
    hydration: float | None,
    hp: float | None,
    max_hp: float | None,
    observations: list[dict] | None = None,
    loyalty: float | None = None,
) -> dict[str, float]:
    """Full drive vector for one Soul. Pure function of its inputs.

    loyalty: the stored souls.loyalty scalar (issue #31) when known;
    None keeps the nature-baseline fallback.
    """
    drives = {
        drive: nature_baseline(nature, drive) for drive in DRIVES if drive != "survival"
    }
    drives["survival"] = survival_drive(satiety, hydration, hp, max_hp)
    if loyalty is not None:
        drives["loyalty"] = min(1.0, max(0.0, float(loyalty)))
    drives["fear"] = fear_from_observations(observations or [])
    return drives
