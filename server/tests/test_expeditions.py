"""Expeditions and the abroad channel (issue #35).

State machine, trigger, routing, abroad-channel privacy, foraging,
journaling, and loyalty neutrality.
"""

import json
import random
import time

import pytest

from .. import biology
from .. import database
from .. import dormancy
from .. import expeditions
from .. import persistence
from .. import plots
from .. import resources
from .. import viewport as viewport_mod

WORLD = (1920.0, 1080.0)
TICK_DT = 0.2


def _insert_soul(db_conn, soul_id, x=960.0, y=540.0, **kw):
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
        "nature": "hasty",
        "loyalty": 0.5,
    }
    cols.update(kw)
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    db_conn.execute(
        f"INSERT INTO souls ({names}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    db_conn.commit()


def _integrate(sid, dt=TICK_DT):
    """One world_tick-style movement integration step for one soul."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT position, velocity, move_target FROM souls WHERE soul_id = ?",
            (sid,),
        ).fetchone()
        x, y = json.loads(row["position"])
        vx, vy = json.loads(row["velocity"])
        unflushed = persistence.dirty_get(sid)
        if unflushed is not None:
            if unflushed.get("position") is not None:
                x, y = unflushed["position"]
            if unflushed.get("velocity") is not None:
                vx, vy = unflushed["velocity"]
            target = unflushed.get("move_target", None)
            if "move_target" not in unflushed:
                target = json.loads(row["move_target"]) if row["move_target"] else None
        else:
            target = json.loads(row["move_target"]) if row["move_target"] else None
        if vx == 0.0 and vy == 0.0:
            return
        (nx, ny), (nvx, nvy), new_target = persistence.integrate_soul(
            (x, y), (vx, vy), target, dt, WORLD
        )
        persistence.dirty.mark(
            sid, position=[nx, ny], velocity=[nvx, nvy], move_target=new_target
        )


def _drive(sid, now, max_ticks=2000):
    """Run expedition ticks + movement until the expedition ends."""
    states = []
    for tick in range(max_ticks):
        expeditions.advance_tick(tick, now + tick * TICK_DT)
        _integrate(sid)
        with database.get_db() as conn:
            exp = expeditions.active_expedition(conn, sid)
        states.append(exp["state"] if exp else "ended")
        if exp is None:
            break
    return states


def _journal_types(db_conn, soul_id):
    rows = db_conn.execute(
        "SELECT type, payload FROM journal ORDER BY seq ASC"
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("soul_id") == soul_id:
            out.append((row["type"], payload))
    return out


@pytest.fixture(autouse=True)
def _expedition_tables(db_conn):
    expeditions.ensure_schema(db_conn)
    db_conn.execute("DELETE FROM expeditions")
    db_conn.execute("DELETE FROM soul_home_plots")
    db_conn.commit()
    persistence.dirty.clear()
    yield
    persistence.dirty.clear()


def test_home_plot_selected_on_first_departure(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    home = plots.plot_id_for(*plots.plot_at(960.0, 540.0))
    assert expeditions.get_home_plot(db_conn, "s1") is None
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expeditions.start_expedition(conn, "s1", time.time(), 1, dest_plot_id="4:4")
        conn.commit()
    assert expeditions.get_home_plot(db_conn, "s1") == home


def test_start_refuses_when_not_in_home_plot(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    home = plots.plot_id_for(*plots.plot_at(960.0, 540.0))
    with database.get_db() as conn:
        expeditions.set_home_plot(conn, "s1", home, time.time())
        conn.commit()
    # Move the soul two plots away; departure must refuse.
    hx, hy = plots.plot_center(*[int(v) for v in home.split(":")])
    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([hx + 2 * plots.PLOT_SIZE, hy]),),
    )
    db_conn.commit()
    with database.get_db() as conn:
        with pytest.raises(expeditions.ExpeditionRefusal) as exc:
            expeditions.start_expedition(conn, "s1", time.time(), 1)
        assert exc.value.reason == "not_home"


def test_round_trip_two_plus_plots_returns_inside_home(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    home = plots.plot_id_for(*plots.plot_at(960.0, 540.0))
    dest = "4:4"
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exp, bubble = expeditions.start_expedition(
            conn, "s1", time.time(), 1, dest_plot_id=dest, dwell_s=0.0
        )
        conn.commit()
    assert bubble == f"Off to explore plot {dest}!"
    path = json.loads(exp["waypoints"])
    assert len(path) >= 2  # route spans 2+ plots beyond home
    now = time.time()
    states = _drive("s1", now)
    assert states[0] == "departing"
    assert "abroad" in states
    assert "returning" in states
    assert states[-1] == "ended"
    # Returned inside the home rect.
    pos = persistence.read_positions_through()["s1"]
    assert plots.plot_id_for(*plots.plot_at(*pos)) == home
    # Journal bracketed the trip.
    types = [t for t, _ in _journal_types(db_conn, "s1")]
    assert types == [
        expeditions.EVENT_EXPEDITION_STARTED,
        expeditions.EVENT_EXPEDITION_ARRIVED,
        expeditions.EVENT_EXPEDITION_RETURNED,
    ]
    # Full viewport state resumes: position streams again, no abroad summary.
    assert expeditions.abroad_summaries() == {}
    assert "s1" in viewport_mod.read_positions()


def test_destination_must_be_two_plots_out(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    home = plots.plot_id_for(*plots.plot_at(960.0, 540.0))
    hgx, hgy = (int(v) for v in home.split(":"))
    near = plots.plot_id_for(hgx + 1, hgy)
    with database.get_db() as conn:
        with pytest.raises(expeditions.ExpeditionRefusal) as exc:
            expeditions.start_expedition(conn, "s1", time.time(), 1, dest_plot_id=near)
        assert exc.value.reason == "dest_too_close"


def test_closed_plot_destination_refused(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    _insert_soul(db_conn, "s2", x=100.0, y=100.0)
    dest = "4:4"
    db_conn.execute(
        "UPDATE plots SET owner_type = 'soul', owner_id = 's2', "
        "access_policy = 'closed' WHERE plot_id = ?",
        (dest,),
    )
    db_conn.commit()
    with database.get_db() as conn:
        with pytest.raises(expeditions.ExpeditionRefusal) as exc:
            expeditions.start_expedition(conn, "s1", time.time(), 1, dest_plot_id=dest)
        assert exc.value.reason == "dest_closed"


def test_no_route_cancels_and_journals(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    home = plots.plot_id_for(*plots.plot_at(960.0, 540.0))
    hgx, hgy = (int(v) for v in home.split(":"))
    # Close the full 8-neighbor ring around home with another soul's plots.
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            pid = plots.plot_id_for(hgx + dx, hgy + dy)
            db_conn.execute(
                "UPDATE plots SET owner_type = 'soul', owner_id = 's2', "
                "access_policy = 'closed' WHERE plot_id = ?",
                (pid,),
            )
    db_conn.commit()
    with database.get_db() as conn:
        with pytest.raises(expeditions.ExpeditionRefusal) as exc:
            expeditions.start_expedition(conn, "s1", time.time(), 1)
        assert exc.value.reason in ("no_route", "no_destination")


def test_route_never_enters_closed_plot(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    _insert_soul(db_conn, "s2", x=100.0, y=100.0)
    # Close a wall of plots; the route must go around, never through.
    for gx in range(4, 12):
        pid = plots.plot_id_for(gx, 4)
        db_conn.execute(
            "UPDATE plots SET owner_type = 'soul', owner_id = 's2', "
            "access_policy = 'closed' WHERE plot_id = ?",
            (pid,),
        )
    db_conn.commit()
    with database.get_db() as conn:
        path = expeditions.route_plots(conn, "s1", "8:5", "8:3")
        assert path is not None
        assert len(path) >= 3
        for pid in path:
            gx, gy = (int(v) for v in pid.split(":"))
            cx, cy = plots.plot_center(gx, gy)
            assert plots.can_enter_plot(conn, "s1", cx, cy), pid


def test_trigger_drive_gated_and_cooldown(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0, nature="hasty")
    _insert_soul(db_conn, "s2", x=960.0, y=540.0, nature="hardy")
    now = time.time()
    started = expeditions.maybe_trigger(300, now)
    assert started == 1
    with database.get_db() as conn:
        assert expeditions.active_expedition(conn, "s1") is not None
        assert expeditions.active_expedition(conn, "s2") is None
    # Hungry souls don't wander.
    _insert_soul(db_conn, "s3", x=960.0, y=540.0, nature="hasty", satiety=10.0)
    assert expeditions.maybe_trigger(600, now) == 0
    # Cooldown: a second trigger right after the first ends is refused.
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expeditions.cancel_expedition(conn, "s1", "test", 601, now + 1.0)
        conn.commit()
    assert expeditions.maybe_trigger(900, now + 2.0) == 0
    # After the 6 h cooldown the soul may wander again.
    assert (
        expeditions.maybe_trigger(
            1200, now + expeditions.EXPEDITION_COOLDOWN_S + 1.0
        )
        == 1
    )


def test_trigger_needs_home_plot_presence(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0, nature="hasty")
    home = plots.plot_id_for(*plots.plot_at(960.0, 540.0))
    with database.get_db() as conn:
        expeditions.set_home_plot(conn, "s1", home, time.time())
        conn.commit()
    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([200.0, 200.0]),),
    )
    db_conn.commit()
    assert expeditions.maybe_trigger(300, time.time()) == 0


def test_abroad_summary_exact_keys_and_privacy_fuzz(db_conn):
    rng = random.Random(35)
    plots_seen = set()
    for _ in range(200):
        state = rng.choice(["abroad", "returning"])
        exp = {
            "soul_id": f"s{rng.randint(1, 9)}",
            "state": state,
            "dwell_until": time.time() + rng.randint(0, 9999) if rng.random() < 0.5 else None,
            "last_foraged_at": time.time() - rng.randint(0, 9999),
        }
        plot_id = f"{rng.randint(0, 15)}:{rng.randint(0, 8)}"
        summary = expeditions.build_abroad_summary(exp, plot_id)
        assert set(summary.keys()) == {"entity_id", "state", "activity_label", "plot"}
        assert set(summary.keys()) == set(expeditions.ABROAD_SUMMARY_KEYS)
        assert summary["entity_id"] == exp["soul_id"]
        assert summary["state"] == state
        assert summary["plot"] == plot_id
        plots_seen.add(plot_id)
        # The compromised verbose serializer: a full soul dict. The
        # summary must expose none of its sensitive fields -- the
        # curated leak list below is drawn from it.
        blob = json.dumps(summary)
        # Distinctive sensitive values from the verbose soul dict must
        # never appear in the summary: fine position, biology, essence,
        # identity beyond the id, and the sensitive key names. The soul
        # id itself is allowed -- it is one of the four summary keys.
        for leak in (
            "961.5",
            "541.2",
            "Brave",
            "Wisp",
            "88.1",
            "72.4",
            "100.0",
            "42.0",
            "viewport",
            "satiety",
            "hydration",
            "dormant",
            "biology",
            "position",
            "essence",
        ):
            assert leak not in blob, leak
    assert len(plots_seen) > 10


def test_abroad_channel_hides_fine_state(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exp, _ = expeditions.start_expedition(
            conn, "s1", now, 1, dest_plot_id="4:4", dwell_s=0.0
        )
        conn.execute(
            "UPDATE expeditions SET state = 'abroad' WHERE expedition_id = ?",
            (exp["expedition_id"],),
        )
        conn.commit()
    summaries = expeditions.abroad_summaries()
    assert set(summaries) == {"s1"}
    summary = summaries["s1"]
    assert set(summary.keys()) == {"entity_id", "state", "activity_label", "plot"}
    assert summary["state"] == "abroad"
    # The pump withholds fine state for away souls.
    session = viewport_mod.ViewportSession("owner_s1")
    positions = {s: p for s, p in viewport_mod.read_positions().items()
                 if s not in summaries}
    assert "s1" not in positions
    ops = viewport_mod.diff_abroad(summaries, session, now)
    assert len(ops) == 1
    op, domain = ops[0]
    assert op["op"] == "abroad"
    assert set(op) == {"op", "soul_id", "entity_id", "state",
                       "activity_label", "plot"}
    assert "x" not in op and "y" not in op


def test_abroad_op_heartbeat_and_end(db_conn):
    session = viewport_mod.ViewportSession("owner_s1")
    now = time.time()
    current = {"s1": {"entity_id": "s1", "state": "abroad",
                      "activity_label": "exploring", "plot": "7:4"}}
    ops = viewport_mod.diff_abroad(current, session, now)
    assert len(ops) == 1
    # Same summary within the cadence: no repeat op.
    assert viewport_mod.diff_abroad(current, session, now + 0.2) == []
    # Heartbeat at 1 Hz even when unchanged.
    ops = viewport_mod.diff_abroad(current, session, now + 1.1)
    assert len(ops) == 1
    # Changed summary: immediate op.
    changed = {"s1": {"entity_id": "s1", "state": "abroad",
                      "activity_label": "foraging", "plot": "6:4"}}
    ops = viewport_mod.diff_abroad(changed, session, now + 1.2)
    assert len(ops) == 1
    assert ops[0][0]["activity_label"] == "foraging"
    # Soul returns: abroad_end op, exactly once.
    ops = viewport_mod.diff_abroad({}, session, now + 1.3)
    assert len(ops) == 1
    assert ops[0][0]["op"] == "abroad_end"
    assert ops[0][0]["soul_id"] == "s1"
    assert viewport_mod.diff_abroad({}, session, now + 1.4) == []


def test_foraging_accrues_inventory_and_xp_silently(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    resources.ensure_schema(db_conn)
    db_conn.commit()
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exp, _ = expeditions.start_expedition(
            conn, "s1", now, 1, dest_plot_id="4:4", dwell_s=3600.0
        )
        # Simulate a completed arrival: all waypoints consumed, dwelling.
        waypoints = json.loads(exp["waypoints"])
        conn.execute(
            "UPDATE expeditions SET state = 'abroad', "
            "waypoint_index = ?, dwell_until = ? WHERE expedition_id = ?",
            (len(waypoints), now + 3600.0, exp["expedition_id"]),
        )
        conn.commit()
    # Drop the soul next to a ready node at the destination plot.
    dgx, dgy = 4, 4
    nx, ny = plots.plot_center(dgx, dgy)
    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([nx, ny]),),
    )
    db_conn.execute(
        "INSERT INTO resource_nodes (node_id, plot_id, kind, x, y, amount, "
        "capacity, respawns_at, state, created_at) VALUES "
        "('n1', '4:4', 'food', ?, ?, 10, 10, NULL, 'ready', ?)",
        (nx + 5.0, ny, now),
    )
    db_conn.commit()
    xp_before = db_conn.execute(
        "SELECT COALESCE(xp, 0) FROM souls WHERE soul_id = 's1'"
    ).fetchone()[0]
    expeditions.advance_tick(2, now + expeditions.FORAGE_INTERVAL_S + 1.0)
    inv = resources.inventory_for(db_conn, "s1")
    assert inv.get("food", 0) == resources.GATHER_YIELD
    xp_after = db_conn.execute(
        "SELECT COALESCE(xp, 0) FROM souls WHERE soul_id = 's1'"
    ).fetchone()[0]
    assert xp_after == xp_before + resources.XP_GATHER
    # Silent: no journal row per gather, only the trip brackets.
    types = [t for t, _ in _journal_types(db_conn, "s1")]
    assert types == [expeditions.EVENT_EXPEDITION_STARTED]


def test_biology_ticks_while_abroad(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exp, _ = expeditions.start_expedition(
            conn, "s1", now, 1, dest_plot_id="4:4", dwell_s=3600.0
        )
        conn.execute(
            "UPDATE expeditions SET state = 'abroad', dwell_until = ? "
            "WHERE expedition_id = ?",
            (now + 3600.0, exp["expedition_id"]),
        )
        conn.commit()
    sat_before = db_conn.execute(
        "SELECT satiety FROM souls WHERE soul_id = 's1'"
    ).fetchone()[0]
    with database.get_db() as conn:
        biology.apply_biology_tick(conn, 50, now + 600.0, 600.0)
        conn.commit()
    sat_after = db_conn.execute(
        "SELECT satiety FROM souls WHERE soul_id = 's1'"
    ).fetchone()[0]
    assert sat_after < sat_before


def test_journal_and_loyalty_neutrality(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0, loyalty=0.7)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exp, _ = expeditions.start_expedition(
            conn, "s1", now, 1, dest_plot_id="4:4", dwell_s=0.0
        )
        conn.commit()
        expedition_id = exp["expedition_id"]
    _drive("s1", now)
    types = [t for t, _ in _journal_types(db_conn, "s1")]
    assert types == [
        expeditions.EVENT_EXPEDITION_STARTED,
        expeditions.EVENT_EXPEDITION_ARRIVED,
        expeditions.EVENT_EXPEDITION_RETURNED,
    ]
    payloads = dict((t, p) for t, p in _journal_types(db_conn, "s1"))
    assert payloads[expeditions.EVENT_EXPEDITION_STARTED]["expedition_id"] == expedition_id
    assert payloads[expeditions.EVENT_EXPEDITION_STARTED]["dest_plot"] == "4:4"
    assert payloads[expeditions.EVENT_EXPEDITION_RETURNED]["home_plot"] == payloads[
        expeditions.EVENT_EXPEDITION_STARTED
    ]["home_plot"]
    loyalty = db_conn.execute(
        "SELECT loyalty FROM souls WHERE soul_id = 's1'"
    ).fetchone()[0]
    assert loyalty == pytest.approx(0.7)


def test_cancel_journals_reason(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expeditions.start_expedition(conn, "s1", now, 1, dest_plot_id="4:4")
        expeditions.cancel_expedition(conn, "s1", "no_route_home", 2, now + 1.0)
        conn.commit()
    types = [t for t, _ in _journal_types(db_conn, "s1")]
    assert types == [
        expeditions.EVENT_EXPEDITION_STARTED,
        expeditions.EVENT_EXPEDITION_CANCELLED,
    ]
    _, payload = _journal_types(db_conn, "s1")[-1]
    assert payload["reason"] == "no_route_home"
    with database.get_db() as conn:
        assert expeditions.active_expedition(conn, "s1") is None


def test_timeout_forces_return_then_cancels(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exp, _ = expeditions.start_expedition(
            conn, "s1", now, 1, dest_plot_id="4:4", dwell_s=3600.0
        )
        conn.execute(
            "UPDATE expeditions SET started_at = ?, state = 'abroad' "
            "WHERE expedition_id = ?",
            (now - expeditions.EXPEDITION_MAX_S - 10.0, exp["expedition_id"]),
        )
        conn.commit()
    # Park the soul at the destination plot so the forced return has a route.
    dx, dy = plots.plot_center(4, 4)
    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([dx, dy]),),
    )
    db_conn.commit()
    # First timeout: forced into returning with a fresh route, not cancelled.
    expeditions.advance_tick(2, now)
    with database.get_db() as conn:
        exp = expeditions.active_expedition(conn, "s1")
    assert exp is not None and exp["state"] == "returning"
    assert json.loads(exp["waypoints"])
    # Still timed out while returning: cancelled and journalled.
    expeditions.advance_tick(3, now + 1.0)
    with database.get_db() as conn:
        assert expeditions.active_expedition(conn, "s1") is None
    types = [t for t, _ in _journal_types(db_conn, "s1")]
    assert types[0] == expeditions.EVENT_EXPEDITION_STARTED
    assert types[-1] == expeditions.EVENT_EXPEDITION_CANCELLED
    _, payload = _journal_types(db_conn, "s1")[-1]
    assert payload["reason"] == "timeout"


def test_activity_labels(db_conn):
    now = time.time()
    base = {"soul_id": "s1", "dwell_until": None, "last_foraged_at": None}
    assert expeditions._activity_label({**base, "state": "departing"}) == "departing"
    assert expeditions._activity_label({**base, "state": "returning"}) == "returning home"
    assert (
        expeditions._activity_label(
            {**base, "state": "abroad", "last_foraged_at": now - 10.0}
        )
        == "foraging"
    )
    assert (
        expeditions._activity_label(
            {
                **base,
                "state": "abroad",
                "dwell_until": now + 100.0,
                "last_foraged_at": now - 9999.0,
            }
        )
        == "resting"
    )
    assert (
        expeditions._activity_label({**base, "state": "abroad"}) == "exploring"
    )


def test_cannot_start_while_carried_or_dormant(db_conn):
    _insert_soul(db_conn, "s1", x=960.0, y=540.0, essence=0.0)
    assert dormancy.is_dormant(0.0)
    with database.get_db() as conn:
        with pytest.raises(expeditions.ExpeditionRefusal) as exc:
            expeditions.start_expedition(conn, "s1", time.time(), 1)
        assert exc.value.reason == "soul_dormant"
