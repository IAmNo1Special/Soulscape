"""Tick-side adjudication of reflex consume intents (issue #24).

The reflex layer emits `eat` / `drink` when a needy soul is within
reach of known food/water. These are the only agent-pool intents the
#14/#17 pump does not already route: move_to goes through the normal
move adjudication; consume intents land here.

Revalidation at adjudication (defense in depth behind the pool's
pre-enqueue check): the soul must exist, be awake and uncollapsed,
and the food/water must STILL be at the payload position -- gone
means the intent is stale and is rejected into the journal.

Until #34 ships real resource nodes the production provider returns
none, so consume intents are unreachable in production; the scenario
sim (tests) drives this path with a stub provider. Food is not
consumed in v0: #34 will own yield counts and respawn.
"""

import json
import math

from .. import biology, database, dormancy, persistence

#: Intent kinds routed to adjudicate_consume by the tick pump.
CONSUME_KINDS = frozenset({"eat", "drink"})

#: Biology gain per consume action (v0: fixed, no items yet).
EAT_SATIETY_GAIN = 40.0
DRINK_HYDRATION_GAIN = 40.0


def _validate_eat(payload: dict) -> tuple[float, float] | None:
    food = payload.get("food")
    return _as_pair(food)


def _validate_drink(payload: dict) -> tuple[float, float] | None:
    water = payload.get("water")
    return _as_pair(water)


def _as_pair(raw) -> tuple[float, float] | None:
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return (x, y)


def adjudicate_consume(tick, intent: dict, provider) -> None:
    """Adjudicate one eat/drink intent inside the tick pump.

    `tick` is the WorldTick (uses tick.tick_id and tick._reject).
    `provider` answers whether the food/water is still there.
    """
    intent_id = intent["intent_id"]
    soul_id = intent["soul_id"]
    kind = intent["kind"]
    payload = intent["payload"] or {}
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT state, COALESCE(essence, 0.0) AS essence, "
            "COALESCE(satiety, 100.0) AS satiety, "
            "COALESCE(hydration, 100.0) AS hydration "
            "FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
        if row is None:
            tick._reject(conn, intent, "soul_not_found")
            return
        if (row["state"] or biology.STATE_NORMAL) == biology.STATE_COLLAPSED:
            tick._reject(conn, intent, "collapsed")
            return
        if dormancy.is_dormant(row["essence"]):
            tick._reject(conn, intent, "soul_dormant")
            return
        if kind == "eat":
            target = _validate_eat(payload)
            column, gain, present = (
                "satiety",
                EAT_SATIETY_GAIN,
                provider.is_food_at(*target) if target else False,
            )
        elif kind == "drink":
            target = _validate_drink(payload)
            column, gain, present = (
                "hydration",
                DRINK_HYDRATION_GAIN,
                provider.is_water_at(*target) if target else False,
            )
        else:
            tick._reject(conn, intent, "unknown_kind")
            return
        if target is None:
            tick._reject(conn, intent, "bad_payload")
            return
        if not present:
            tick._reject(conn, intent, "target_gone")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f"UPDATE souls SET {column} = MIN(100.0, {column} + ?) "
                "WHERE soul_id = ?",
                (gain, soul_id),
            )
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                (
                    "adjudicated",
                    json.dumps({"kind": kind, "gain": gain, "at": list(target)}),
                    intent_id,
                ),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                persistence.EVENT_INTENT_ADJUDICATED,
                {
                    "intent_id": intent_id,
                    "kind": kind,
                    "soul_id": soul_id,
                    "params": {"gain": gain, "at": list(target)},
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
