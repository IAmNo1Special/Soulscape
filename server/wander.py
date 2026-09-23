"""Baseline idle wander for the Hub world tick.

Offline souls roam continuously via the client's local physics; online
souls are Hub-authoritative, and the Hub only moved souls when a brain
fired (reflex on hunger/fear, LLM deliberation, or the JEV explore
tier). A healthy idle soul with JEV/LLM down or outbid by higher
goals therefore sat still indefinitely.

This module is the no-LLM fallback: every tick, idle souls get a
small probabilistic chance to amble to a nearby point, at the same
JEV_AMBLE_SPEED the explore/socialize/sate moves use so trips span
several viewport pump frames and render as glides, not teleports.

Design notes:
- Intents, not direct writes: wander goes through the agent pool's
  validate_and_enqueue path (custody-correct, revalidated) and is
  adjudicated on a later tick like every other move, so the journal
  and replay see ordinary move_to intents. The *decision* to wander
  is sim-driven and intentionally out of replay scope, exactly like
  the agent pool and the JEV worker (see determinism.py).
- Yield to real cognition: dormant/collapsed/carried/abroad souls
  never wander; needy souls (below NEED_BAND_LOW) are left for the
  foraging reflex and JEV sate planning; souls with an active JEV
  wander episode keep it; a per-soul cooldown prevents duplicate
  enqueues in the tick gap between enqueue and adjudication.
- Bounded: at most WANDER_MAX_PER_TICK souls per tick, mirroring the
  think budget, so a 500-soul world does not intent-spam.
"""

from __future__ import annotations

import logging
import math
import random
import time

from . import biology
from . import database
from . import dormancy
from . import persistence

logger = logging.getLogger("soulscape_hub")

#: Master switch for the tick-driven sweep. True in production; the
#: test-suite conftests force it False (with restore) so world-stepping
#: tests stay deterministic -- a 2%/step unseeded wander would
#: otherwise move idle souls out from under position assertions.
#: sweep() itself ignores this flag; test_wander.py exercises the
#: sweep directly.
ENABLED = True

#: Per-tick wander chance per idle soul. At 20 Hz this averages one
#: wander per ~10 s of idleness, in the spirit of the offline
#: 1-3 s roam pauses plus travel time.
IDLE_WANDER_PROB_PER_TICK = 0.005

#: Upper bound on wander intents enqueued per tick (think-budget twin).
WANDER_MAX_PER_TICK = 4

#: Wander leg length in world units (JEV legs are 80-150).
WANDER_MIN_DIST_U = 80.0
WANDER_MAX_DIST_U = 200.0

#: Minimum seconds between two baseline wanders of the same soul.
WANDER_MIN_INTERVAL_S = 5.0


def pick_wander_target(
    x: float,
    y: float,
    rng: random.Random,
    bounds: tuple[float, float],
) -> tuple[float, float]:
    """One random nearby waypoint, clamped to the world margin."""
    from .persistence import POSITION_MARGIN

    angle = rng.uniform(0.0, 2.0 * math.pi)
    dist = rng.uniform(WANDER_MIN_DIST_U, WANDER_MAX_DIST_U)
    tx = min(
        max(x + math.cos(angle) * dist, POSITION_MARGIN),
        bounds[0] - POSITION_MARGIN,
    )
    ty = min(
        max(y + math.sin(angle) * dist, POSITION_MARGIN),
        bounds[1] - POSITION_MARGIN,
    )
    return (tx, ty)


def _parse_pair(raw) -> tuple[float, float]:
    import json

    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        raw = json.loads(raw)
    x, y = float(raw[0]), float(raw[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("non-finite pair")
    return (x, y)


def _last_wander_map(tick) -> dict[str, float]:
    last = getattr(tick, "_baseline_wander_at", None)
    if last is None:
        last = {}
        try:
            setattr(tick, "_baseline_wander_at", last)
        except Exception:
            pass
    return last


def _jev_wandering(tick, soul_id: str) -> bool:
    """True while the JEV tier owns this soul's motion."""
    worker = getattr(tick, "_jev_worker", None)
    if worker is None:
        return False
    wandering = getattr(worker, "wandering", None)
    if wandering is None:
        return False
    try:
        return bool(wandering(soul_id))
    except Exception:
        return False


def sweep(
    tick,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
    prob: float = IDLE_WANDER_PROB_PER_TICK,
    max_per_tick: int = WANDER_MAX_PER_TICK,
) -> int:
    """Enqueue amble wanders for idle souls. Returns the count.

    Never raises: the tick loop wraps this in try/except anyway, but
    a background sweep must not break the world step.
    """
    from . import affection
    from . import expeditions
    from . import plots
    from . import resources

    try:
        now = time.time() if now is None else now
        if rng is None:
            rng = random.Random()
        bounds = database.SCREEN_BOUNDS
        try:
            abroad = set(expeditions.abroad_summaries())
        except Exception:
            abroad = set()
        try:
            carried = affection.carried_souls()
        except Exception:
            carried = set()
        last = _last_wander_map(tick)
        provider = resources.node_provider()

        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT soul_id, position, velocity, move_target, state, "
                "custodian_id, owner_id, "
                "COALESCE(essence, 0.0) AS essence, "
                "COALESCE(satiety, 100.0) AS satiety, "
                "COALESCE(hydration, 100.0) AS hydration "
                "FROM souls"
            ).fetchall()
            candidates: list[tuple[dict, float, float]] = []
            for row in rows:
                try:
                    soul_id = row["soul_id"]
                    if (row["state"] or biology.STATE_NORMAL) == (
                        biology.STATE_COLLAPSED
                    ):
                        continue
                    if dormancy.is_dormant(row["essence"]):
                        continue
                    if soul_id in carried or soul_id in abroad:
                        continue
                    if float(row["satiety"]) < biology.NEED_BAND_LOW:
                        continue
                    if float(row["hydration"]) < biology.NEED_BAND_LOW:
                        continue
                    if _jev_wandering(tick, soul_id):
                        continue
                    if now - last.get(soul_id, 0.0) < WANDER_MIN_INTERVAL_S:
                        continue
                    try:
                        x, y = _parse_pair(row["position"])
                        vx, vy = _parse_pair(row["velocity"])
                    except (ValueError, TypeError, IndexError, KeyError):
                        continue
                    unflushed = persistence.dirty_get(soul_id)
                    if unflushed is not None:
                        if unflushed.get("position") is not None:
                            x, y = (
                                float(unflushed["position"][0]),
                                float(unflushed["position"][1]),
                            )
                        if unflushed.get("velocity") is not None:
                            vx, vy = (
                                float(unflushed["velocity"][0]),
                                float(unflushed["velocity"][1]),
                            )
                    target_raw = (
                        unflushed.get("move_target", None)
                        if unflushed is not None and "move_target" in unflushed
                        else row["move_target"]
                    )
                    if vx != 0.0 or vy != 0.0 or target_raw:
                        continue
                    candidates.append((dict(row), x, y))
                except Exception:
                    logger.exception("wander candidate check failed")
                    continue

            ordered = list(candidates)
            rng.shuffle(ordered)
            made = 0
            for row, x, y in ordered:
                if made >= max_per_tick:
                    break
                if rng.random() >= prob:
                    continue
                soul_id = row["soul_id"]
                tx, ty = None, None
                for _ in range(3):
                    cx, cy = pick_wander_target(x, y, rng, bounds)
                    try:
                        if plots.can_enter_plot(conn, soul_id, cx, cy):
                            tx, ty = cx, cy
                            break
                    except Exception:
                        tx, ty = cx, cy
                        break
                if tx is None:
                    continue
                try:
                    intent_id = tick.agent_pool.validate_and_enqueue(
                        soul_id,
                        "move_to",
                        {"x": tx, "y": ty, "pace": "amble", "wander": True},
                        provider,
                        tick.tick_id,
                    )
                except Exception:
                    logger.exception("wander enqueue failed for %s", soul_id)
                    continue
                if intent_id is not None:
                    last[soul_id] = now
                    made += 1
            return made
    except Exception:
        logger.exception("baseline wander sweep failed")
        return 0
