"""Tick-side adjudication of reflex consume intents (issues #24, #34).

The reflex layer emits `eat` / `drink` when a needy soul carries food /
water in its inventory (gathered from #34 resource nodes via the
`gather` intent). These are the only agent-pool intents the #14/#17
pump does not already route: move_to goes through the normal move
adjudication; gather lands in resources.adjudicate_gather; consume
intents land here.

Revalidation at adjudication (defense in depth behind the pool's
pre-enqueue check): the soul must exist, be awake and uncollapsed, and
must still hold at least one unit of the item -- gone means the intent
is stale and is rejected into the journal.

Eat/drink consume one inventory unit and restore needs; each completion
accrues XP_EAT / XP_DRINK to souls.xp silently (telemetry only).
"""

import json

from .. import biology, database, dormancy, persistence, resources

#: Intent kinds routed to adjudicate_consume by the tick pump.
CONSUME_KINDS = frozenset({"eat", "drink"})

#: Biology gain per consume action (one inventory unit).
EAT_SATIETY_GAIN = 40.0
DRINK_HYDRATION_GAIN = 40.0


def adjudicate_consume(tick, intent: dict, provider=None) -> None:
    """Adjudicate one eat/drink intent inside the tick pump.

    `tick` is the WorldTick (uses tick.tick_id and tick._reject).
    `provider` is legacy (pre-#34 node checks) and unused: consumption
    is from inventory now, validated server-side below.
    """
    intent_id = intent["intent_id"]
    soul_id = intent["soul_id"]
    kind = intent["kind"]
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
            item, column, gain, xp = (
                resources.ITEM_FOOD,
                "satiety",
                EAT_SATIETY_GAIN,
                resources.XP_EAT,
            )
            empty_reason = "no_food"
        elif kind == "drink":
            item, column, gain, xp = (
                resources.ITEM_WATER,
                "hydration",
                DRINK_HYDRATION_GAIN,
                resources.XP_DRINK,
            )
            empty_reason = "no_water"
        else:
            tick._reject(conn, intent, "unknown_kind")
            return
        if resources.inventory_qty(conn, soul_id, item) < 1:
            tick._reject(conn, intent, empty_reason)
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            removed = resources.remove_item(conn, soul_id, item, 1)
            assert removed, "inventory vanished between check and hold"
            conn.execute(
                f"UPDATE souls SET {column} = MIN(100.0, {column} + ?), "
                "xp = COALESCE(xp, 0) + ? WHERE soul_id = ?",
                (gain, xp, soul_id),
            )
            result = {"kind": kind, "item": item, "gain": gain, "xp": xp}
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                ("adjudicated", json.dumps(result), intent_id),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                persistence.EVENT_INTENT_ADJUDICATED,
                {
                    "intent_id": intent_id,
                    "kind": kind,
                    "soul_id": soul_id,
                    "params": result,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
