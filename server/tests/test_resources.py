"""Resource nodes, gathering, inventory, XP, and the economy smoke test.

Issue #34: resource nodes spawn per density/spacing rules with respawn
timers; gather/eat/drink are server-adjudicated activities driven by the
soul's drives; selling flows through the #17 marketplace intents; XP
accrues silently to souls.xp.
"""

import asyncio
import json
import math
import time

import pytest

from .. import biology
from .. import database
from .. import intents
from .. import market
from .. import persistence
from .. import plots
from .. import resources
from ..agents import pool as pool_mod
from ..agents import scheduler as sched_mod
from ..world_tick import WorldTick


def _insert_soul(db_conn, soul_id, x=100.0, y=100.0, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "position": json.dumps([x, y]),
        "velocity": json.dumps([0.0, 0.0]),
        "essence": 100.0,
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "state": "normal",
        "nature": "Hardy",
    }
    cols.update(kw)
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    db_conn.execute(
        f"INSERT INTO souls ({names}) VALUES ({placeholders})",
        tuple(
            json.dumps(v) if isinstance(v, (list, dict)) else v for v in cols.values()
        ),
    )
    db_conn.commit()


def _insert_node(db_conn, node_id, kind, x, y, amount=10):
    resources.ensure_schema(db_conn)
    db_conn.execute(
        "INSERT INTO resource_nodes "
        "(node_id, plot_id, kind, x, y, amount, capacity, "
        "respawns_at, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'ready', ?)",
        (
            node_id,
            "0:0",
            kind,
            x,
            y,
            amount,
            resources.NODE_CAPACITY,
            time.time(),
        ),
    )
    db_conn.commit()


def _seed(db_conn, seed=7):
    with database.get_db() as conn:
        n = resources.seed_resource_nodes(conn, seed=seed)
        conn.commit()
    return n


# ---------------------------------------------------------------------------
# Seeding: density, spacing, determinism, claimed-plot exclusion
# ---------------------------------------------------------------------------


def test_seed_density_and_counts(db_conn):
    n = _seed(db_conn, seed=7)
    assert n == 60  # 144 plots: 36 food (1/4) + 24 water (1/6)
    with database.get_db() as conn:
        food = conn.execute(
            "SELECT COUNT(*) AS c FROM resource_nodes WHERE kind = 'food'"
        ).fetchone()["c"]
        water = conn.execute(
            "SELECT COUNT(*) AS c FROM resource_nodes WHERE kind = 'water'"
        ).fetchone()["c"]
    assert food == 144 // 4
    assert water == 144 // 6


def test_seed_min_spacing(db_conn):
    _seed(db_conn, seed=7)
    with database.get_db() as conn:
        pts = [
            (r["x"], r["y"])
            for r in conn.execute("SELECT x, y FROM resource_nodes").fetchall()
        ]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            assert d >= resources.MIN_NODE_SPACING_WU - 1e-9


def test_seed_deterministic(db_conn):
    _seed(db_conn, seed=7)
    with database.get_db() as conn:
        first = [
            (r["node_id"], r["x"], r["y"])
            for r in conn.execute(
                "SELECT node_id, x, y FROM resource_nodes ORDER BY node_id"
            ).fetchall()
        ]
    db_conn.execute("DELETE FROM resource_nodes")
    db_conn.commit()
    _seed(db_conn, seed=7)
    with database.get_db() as conn:
        second = [
            (r["node_id"], r["x"], r["y"])
            for r in conn.execute(
                "SELECT node_id, x, y FROM resource_nodes ORDER BY node_id"
            ).fetchall()
        ]
    assert first == second


def test_seed_excludes_claimed_plots(db_conn):
    db_conn.execute(
        "UPDATE plots SET owner_id = 's1', owner_type = 'soul' WHERE plot_id = '8:4'"
    )
    db_conn.commit()
    _seed(db_conn, seed=7)
    with database.get_db() as conn:
        hit = conn.execute(
            "SELECT 1 FROM resource_nodes WHERE plot_id = '8:4'"
        ).fetchone()
    assert hit is None


def test_claim_removes_nodes_on_plot(db_conn):
    _insert_soul(db_conn, "s1", essence=10000.0)
    seeded = _seed(db_conn, seed=7)
    assert seeded > 0
    record, created = plots.enqueue_plot_intent(
        "test",
        "claim1",
        None,
        "s1",
        "plot_claim",
        {"claimant_soul_id": "s1", "access_policy": "open"},
    )
    assert created
    tick = WorldTick()
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    claimed = db_conn.execute(
        "SELECT plot_id FROM plots WHERE owner_id = 's1'"
    ).fetchall()
    assert claimed, "claim should have taken a plot"
    for c in claimed:
        hit = db_conn.execute(
            "SELECT 1 FROM resource_nodes WHERE plot_id = ?",
            (c["plot_id"],),
        ).fetchone()
        assert hit is None, f"nodes remain on claimed plot {c['plot_id']}"


# ---------------------------------------------------------------------------
# Node lifecycle on an accelerated clock
# ---------------------------------------------------------------------------


def test_node_lifecycle_deplete_and_respawn(db_conn):
    _insert_soul(db_conn, "s1", x=500.0, y=500.0)
    _insert_node(db_conn, "node:food:life", "food", 500.0, 505.0)
    tick = WorldTick()
    t0 = 1_000_000.0
    for i in range(resources.NODE_CAPACITY):
        intents.enqueue_intent(
            "test",
            f"g{i}",
            None,
            "s1",
            "gather",
            {"node_id": "node:food:life"},
        )
    for intent in intents.pending_intents():
        resources.adjudicate_gather(tick, intent, now=t0)
    with database.get_db() as conn:
        node = resources.get_node(conn, "node:food:life")
    assert node["amount"] == 0
    assert node["state"] == "depleted"
    assert node["respawns_at"] == pytest.approx(t0 + resources.RESPAWN_SECONDS)

    with database.get_db() as conn:
        assert resources.respawn_sweep(conn, t0 + 3600.0) == 0
        node = resources.get_node(conn, "node:food:life")
        assert node["state"] == "depleted"
        assert resources.respawn_sweep(conn, t0 + resources.RESPAWN_SECONDS + 1.0) == 1
        node = resources.get_node(conn, "node:food:life")
    assert node["state"] == "ready"
    assert node["amount"] == node["capacity"] == resources.NODE_CAPACITY
    assert node["respawns_at"] is None


# ---------------------------------------------------------------------------
# Gather adjudication: server-side validation of every step
# ---------------------------------------------------------------------------


def test_gather_happy_path(db_conn):
    _insert_soul(db_conn, "s1", x=500.0, y=500.0)
    _insert_node(db_conn, "node:food:g1", "food", 505.0, 500.0, amount=4)
    record = intents.enqueue_intent(
        "test", "g1", None, "s1", "gather", {"node_id": "node:food:g1"}
    )
    tick = WorldTick()
    resources.adjudicate_gather(tick, record, now=2000.0)
    row = db_conn.execute(
        "SELECT status, result FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    result = json.loads(row["result"])
    assert result["yield"] == 1 and result["node_amount"] == 3
    assert result["xp"] == resources.XP_GATHER
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "s1", "food") == 1
        node = resources.get_node(conn, "node:food:g1")
    assert node["amount"] == 3 and node["state"] == "ready"
    soul = db_conn.execute("SELECT xp FROM souls WHERE soul_id = 's1'").fetchone()
    assert int(soul["xp"]) == resources.XP_GATHER


def test_gather_success_notes_followup_think(db_conn):
    _insert_soul(db_conn, "s1", x=500.0, y=500.0)
    _insert_node(db_conn, "node:food:g2", "food", 505.0, 500.0, amount=4)
    record = intents.enqueue_intent(
        "test", "g2", None, "s1", "gather", {"node_id": "node:food:g2"}
    )
    tick = WorldTick()
    notes: list[tuple] = []
    tick._jev_note = lambda sid, kind, floor_override=None: notes.append(
        (sid, kind, floor_override)
    )
    resources.adjudicate_gather(tick, record, now=2000.0)
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    assert notes == [("s1", "needs_change", resources.FOLLOWUP_NOTE_FLOOR_S)]


def test_gather_rejection_notes_no_followup(db_conn):
    _insert_soul(db_conn, "far", x=0.0, y=0.0)
    _insert_node(db_conn, "node:food:ok", "food", 500.0, 500.0, amount=5)
    record = intents.enqueue_intent(
        "test", "n1", None, "far", "gather", {"node_id": "node:food:ok"}
    )
    tick = WorldTick()
    notes: list[tuple] = []
    tick._jev_note = lambda sid, kind, floor_override=None: notes.append(
        (sid, kind, floor_override)
    )
    resources.adjudicate_gather(tick, record, now=3000.0)
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "rejected"
    assert notes == []


def test_gather_rejections(db_conn):
    _insert_soul(db_conn, "near", x=500.0, y=500.0)
    _insert_soul(db_conn, "far", x=0.0, y=0.0)
    _insert_soul(db_conn, "col", state="collapsed")
    _insert_soul(db_conn, "dor", essence=0.0)
    _insert_node(db_conn, "node:food:ok", "food", 500.0, 500.0, amount=5)
    _insert_node(db_conn, "node:food:empty", "food", 500.0, 500.0, amount=1)
    tick = WorldTick()
    t0 = 3000.0

    def run(soul_id, node_id, nonce, now=t0):
        record = intents.enqueue_intent(
            "test", nonce, None, soul_id, "gather", {"node_id": node_id}
        )
        resources.adjudicate_gather(tick, record, now=now)
        row = db_conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
        return row["status"], json.loads(row["result"] or "{}")

    # Drain the "empty" node first (amount 1 -> 0).
    status, _ = run("near", "node:food:empty", "n0")
    assert status == "adjudicated"

    status, result = run("far", "node:food:ok", "n1")
    assert status == "rejected" and result["reason"] == "too_far"
    status, result = run("near", "node:food:empty", "n2")
    assert status == "rejected" and result["reason"] == "node_depleted"
    status, result = run("near", "node:nope", "n3")
    assert status == "rejected" and result["reason"] == "node_not_found"
    status, result = run("col", "node:food:ok", "n4")
    assert status == "rejected" and result["reason"] == "collapsed"
    status, result = run("dor", "node:food:ok", "n5")
    assert status == "rejected" and result["reason"] == "soul_dormant"
    status, result = run("ghost", "node:food:ok", "n6")
    assert status == "rejected" and result["reason"] == "soul_not_found"
    record = intents.enqueue_intent("test", "n7", None, "near", "gather", {})
    resources.adjudicate_gather(tick, record, now=t0)
    row = db_conn.execute(
        "SELECT status, result FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "rejected"
    assert json.loads(row["result"])["reason"] == "bad_payload"


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------


def test_inventory_add_remove(db_conn):
    _insert_soul(db_conn, "s1")
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "s1", "food") == 0
        assert resources.add_item(conn, "s1", "food", 3) == 3
        assert resources.add_item(conn, "s1", "food", 2) == 5
        assert resources.inventory_for(conn, "s1") == {"food": 5}
        assert resources.remove_item(conn, "s1", "food", 2) is True
        assert resources.inventory_qty(conn, "s1", "food") == 3
        assert resources.remove_item(conn, "s1", "food", 9) is False
        assert resources.inventory_qty(conn, "s1", "food") == 3
        assert resources.remove_item(conn, "s1", "food", 3) is True
        assert resources.inventory_qty(conn, "s1", "food") == 0
        assert resources.inventory_for(conn, "s1") == {}
        conn.commit()
    with pytest.raises(ValueError):
        with database.get_db() as conn:
            resources.add_item(conn, "s1", "rocks", 1)


# ---------------------------------------------------------------------------
# Eat/drink from inventory + XP
# ---------------------------------------------------------------------------


def test_drink_from_inventory(db_conn):
    from ..agents import consume

    _insert_soul(db_conn, "s1", hydration=30.0)
    with database.get_db() as conn:
        resources.add_item(conn, "s1", "water", 1)
        conn.commit()
    record = intents.enqueue_intent("test", "n1", None, "s1", "drink", {})
    tick = WorldTick()
    consume.adjudicate_consume(tick, record)
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    soul = db_conn.execute(
        "SELECT hydration, xp FROM souls WHERE soul_id = 's1'"
    ).fetchone()
    assert float(soul["hydration"]) == pytest.approx(70.0)
    assert int(soul["xp"]) == resources.XP_DRINK
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "s1", "water") == 0


# ---------------------------------------------------------------------------
# Selling through the marketplace intent pipeline
# ---------------------------------------------------------------------------


def _market_item(item, qty):
    return {"type": "resource", "item": item, "qty": qty}


def test_market_sell_resource_end_to_end(db_conn):
    _insert_soul(db_conn, "seller", essence=100.0)
    _insert_soul(db_conn, "buyer", essence=100.0)
    with database.get_db() as conn:
        resources.add_item(conn, "seller", "food", 5)
        conn.commit()

    record, created = market.enqueue_market_intent(
        "test",
        "list1",
        None,
        "seller",
        market.KIND_MARKET_LIST,
        {
            "item": _market_item("food", 3),
            "price": 10.0,
            "seller_soul_id": "seller",
        },
    )
    assert created
    tick = WorldTick()
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    # Listing holds the items out of the seller's inventory.
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "seller", "food") == 2
    listing = db_conn.execute("SELECT listing_id, price FROM marketplace").fetchone()
    assert listing is not None

    buy, _ = market.enqueue_market_intent(
        "test",
        "buy1",
        None,
        "buyer",
        market.KIND_MARKET_BUY,
        {"listing_id": listing["listing_id"], "buyer_soul_id": "buyer"},
    )
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (buy["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"

    # Items moved seller -> buyer; essence moved with the 2% tax.
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "buyer", "food") == 3
        assert resources.inventory_qty(conn, "seller", "food") == 2
    buyer = db_conn.execute(
        "SELECT essence FROM souls WHERE soul_id = 'buyer'"
    ).fetchone()
    seller = db_conn.execute(
        "SELECT essence, xp FROM souls WHERE soul_id = 'seller'"
    ).fetchone()
    assert float(buyer["essence"]) == pytest.approx(90.0)
    assert float(seller["essence"]) == pytest.approx(109.8)
    assert int(seller["xp"]) == resources.XP_SELL
    # Ledger rows: buyer debit, seller credit, fund tax.
    types = {
        r["entry_type"]: r["amount"]
        for r in db_conn.execute(
            "SELECT entry_type, amount FROM ledger WHERE intent_id = ?",
            (buy["intent_id"],),
        ).fetchall()
    }
    assert types[market.LEDGER_DEBIT] == pytest.approx(10.0)
    assert types[market.LEDGER_CREDIT] == pytest.approx(9.8)
    assert types[market.LEDGER_TAX] == pytest.approx(0.2)
    assert db_conn.execute("SELECT COUNT(*) AS c FROM marketplace").fetchone()["c"] == 0


def test_market_cancel_resource_returns_inventory(db_conn):
    _insert_soul(db_conn, "seller", essence=100.0)
    with database.get_db() as conn:
        resources.add_item(conn, "seller", "water", 4)
        conn.commit()
    record, _ = market.enqueue_market_intent(
        "test",
        "list1",
        None,
        "seller",
        market.KIND_MARKET_LIST,
        {
            "item": _market_item("water", 4),
            "price": 8.0,
            "seller_soul_id": "seller",
        },
    )
    tick = WorldTick()
    tick.pump_intents()
    listing_id = json.loads(
        db_conn.execute(
            "SELECT result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()["result"]
    )["listing_id"]
    cancel, _ = market.enqueue_market_intent(
        "test",
        "cancel1",
        None,
        "seller",
        market.KIND_MARKET_CANCEL,
        {"listing_id": listing_id},
    )
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (cancel["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "seller", "water") == 4


def test_market_list_resource_insufficient_inventory(db_conn):
    _insert_soul(db_conn, "seller", essence=100.0)
    record, _ = market.enqueue_market_intent(
        "test",
        "list1",
        None,
        "seller",
        market.KIND_MARKET_LIST,
        {
            "item": _market_item("food", 3),
            "price": 10.0,
            "seller_soul_id": "seller",
        },
    )
    tick = WorldTick()
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status, result FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "rejected"
    assert json.loads(row["result"])["reason"] == "insufficient_inventory"


# ---------------------------------------------------------------------------
# XP merge: server-side awards must not break the client's soul sync
# ---------------------------------------------------------------------------


def test_soul_sync_keeps_server_awarded_xp(client, db_conn):
    # The hub awards XP server-side (#34), so the stored value can exceed
    # what the client last saw. The client's next sync (carrying the stale
    # value) must be accepted with the server value kept, not rejected.
    payload = {
        "owner_id": "o1",
        "souls": [{"soul_id": "xp1", "name": "X", "xp": 100.0}],
    }
    assert client.post("/souls", json=payload).status_code == 200
    with database.get_db() as conn:
        resources.award_xp(conn, "xp1", resources.XP_GATHER)
        conn.commit()
    res = client.post("/souls", json=payload)
    assert res.status_code == 200
    assert res.json()["skipped"] == []
    row = db_conn.execute("SELECT xp FROM souls WHERE soul_id = 'xp1'").fetchone()
    assert float(row["xp"]) == 100.0 + resources.XP_GATHER


# ---------------------------------------------------------------------------
# Economy smoke test: 24 h of autonomous forage on an accelerated clock
# ---------------------------------------------------------------------------


def test_economy_smoke_24h_autonomous_forage(db_conn):
    from .. import world as world_mod

    seed = 7
    _seed(db_conn, seed=seed)
    cx, cy = plots.commons_center()
    _insert_soul(
        db_conn,
        "s1",
        x=cx,
        y=cy,
        satiety=15.0,
        hydration=15.0,
        essence=100.0,
    )
    tick = WorldTick()
    provider = resources.node_provider()
    p = pool_mod.AgentPool(think_scheduler=sched_mod.ThinkScheduler(seed=11))
    vision = world_mod.WorldVision()

    # Drain one food node through the real gather path so the run must
    # also regrow a node (respawn 6 h < 24 h sim).
    with database.get_db() as conn:
        doomed = resources.nearest_node(conn, resources.KIND_FOOD, cx, cy)
    assert doomed is not None
    doomed_id = doomed["node_id"]
    dx, dy = doomed["x"], doomed["y"]
    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([dx, dy]),),
    )
    db_conn.commit()
    t0 = time.time()
    for i in range(resources.NODE_CAPACITY):
        intents.enqueue_intent(
            "drain", f"d{i}", None, "s1", "gather", {"node_id": doomed_id}
        )
    for intent in intents.pending_intents():
        resources.adjudicate_gather(tick, intent, now=t0)
    with database.get_db() as conn:
        assert resources.get_node(conn, doomed_id)["state"] == "depleted"
    with database.get_db() as conn:
        resources.remove_item(conn, "s1", "food", resources.NODE_CAPACITY)
        conn.commit()
    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([cx, cy]),),
    )
    db_conn.commit()

    step_s = 600.0
    steps = 144  # 24 h
    min_sat, min_hyd = 100.0, 100.0
    for step in range(steps):
        now = t0 + step * step_s
        vision.rebuild()
        asyncio.run(p.think("s1", vision, provider, tick.tick_id, now))
        tick.pump_intents()
        row = db_conn.execute(
            "SELECT position, velocity, move_target FROM souls WHERE soul_id = 's1'"
        ).fetchone()
        state = {
            "s1": {
                "position": list(json.loads(row["position"])),
                "velocity": list(json.loads(row["velocity"] or "[0, 0]")),
                "move_target": (
                    list(json.loads(row["move_target"])) if row["move_target"] else None
                ),
            }
        }
        persistence.analytic_advance(state, step_s, database.SCREEN_BOUNDS)
        entry = state["s1"]
        db_conn.execute(
            "UPDATE souls SET position = ?, velocity = ?, move_target = ? "
            "WHERE soul_id = 's1'",
            (
                json.dumps(entry["position"]),
                json.dumps(entry["velocity"]),
                (json.dumps(entry["move_target"]) if entry["move_target"] else None),
            ),
        )
        db_conn.commit()
        persistence.dirty.clear()
        with database.get_db() as conn:
            biology.apply_biology_decay(conn, step_s, now + step_s, tick.tick_id)
            conn.commit()
        with database.get_db() as conn:
            resources.respawn_sweep(conn, now + step_s)
            conn.commit()
        row = db_conn.execute(
            "SELECT satiety, hydration, xp, state FROM souls WHERE soul_id = 's1'"
        ).fetchone()
        min_sat = min(min_sat, float(row["satiety"]))
        min_hyd = min(min_hyd, float(row["hydration"]))
        assert row["state"] == "normal"
        tick.tick_id += 1

    # The soul survived on autonomous forage alone: no hand-feeding.
    assert min_sat > 0.0, f"starved: min satiety {min_sat}"
    assert min_hyd > 0.0, f"dehydrated: min hydration {min_hyd}"
    xp = int(
        db_conn.execute("SELECT xp FROM souls WHERE soul_id = 's1'").fetchone()["xp"]
    )
    assert xp > 0, "no XP accrued over the simulated day"
    # The drained node regrew mid-run.
    with database.get_db() as conn:
        node = resources.get_node(conn, doomed_id)
    assert node["state"] == "ready"
    assert node["amount"] == node["capacity"]

    # One sale through the marketplace intent pipeline with ledger rows.
    _insert_soul(db_conn, "buyer", essence=100.0)
    with database.get_db() as conn:
        resources.add_item(conn, "s1", "food", 3)
        conn.commit()
    record, _ = market.enqueue_market_intent(
        "smoke",
        "sell1",
        None,
        "s1",
        market.KIND_MARKET_LIST,
        {
            "item": _market_item("food", 3),
            "price": 12.0,
            "seller_soul_id": "s1",
        },
    )
    tick.pump_intents()
    listing_id = json.loads(
        db_conn.execute(
            "SELECT result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()["result"]
    )["listing_id"]
    buy, _ = market.enqueue_market_intent(
        "smoke",
        "buy1",
        None,
        "buyer",
        market.KIND_MARKET_BUY,
        {"listing_id": listing_id, "buyer_soul_id": "buyer"},
    )
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (buy["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    ledger_n = db_conn.execute(
        "SELECT COUNT(*) AS c FROM ledger WHERE intent_id = ?",
        (buy["intent_id"],),
    ).fetchone()["c"]
    assert ledger_n == 3  # debit + credit + tax
    with database.get_db() as conn:
        assert resources.inventory_qty(conn, "buyer", "food") == 3
    xp_after = int(
        db_conn.execute("SELECT xp FROM souls WHERE soul_id = 's1'").fetchone()["xp"]
    )
    assert xp_after == xp + resources.XP_SELL
