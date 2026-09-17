"""Tests for issue #16: write-behind, typed journal, snapshots, boot recovery."""

import json
import math
import time

import pytest

from .. import database
from .. import intents
from .. import persistence
from .. import viewport
from ..world_tick import INTENT_MOVE_SPEED, TICK_DT, WorldTick


def _insert_soul(db_conn, soul_id, x=100.0, y=100.0, vx=0.0, vy=0.0, target=None):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, move_target) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            soul_id,
            f"owner_{soul_id}",
            json.dumps([x, y]),
            json.dumps([vx, vy]),
            json.dumps(target) if target is not None else None,
        ),
    )
    db_conn.commit()


def _db_state(soul_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT position, velocity, move_target FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    return (
        json.loads(row["position"]),
        json.loads(row["velocity"] or "[0, 0]"),
        json.loads(row["move_target"]) if row["move_target"] else None,
    )


def _through_state(soul_id):
    pos, vel, target = _db_state(soul_id)
    unflushed = persistence.dirty_get(soul_id)
    if unflushed is not None:
        if unflushed.get("position") is not None:
            pos = [float(unflushed["position"][0]), float(unflushed["position"][1])]
        if unflushed.get("velocity") is not None:
            vel = [float(unflushed["velocity"][0]), float(unflushed["velocity"][1])]
        if "move_target" in unflushed:
            raw = unflushed["move_target"]
            target = None if raw is None else [float(raw[0]), float(raw[1])]
    return pos, vel, target


def _journal_rows(intent_id=None):
    with database.get_db() as conn:
        if intent_id is None:
            rows = conn.execute("SELECT seq, tick_id, type, payload FROM journal").fetchall()
        else:
            rows = conn.execute(
                "SELECT seq, tick_id, type, payload FROM journal "
                "WHERE payload LIKE ?",
                (f'%"intent_id": "{intent_id}"%',),
            ).fetchall()
    return [dict(r) for r in rows]


def test_step_marks_dirty_without_db_write(db_conn):
    _insert_soul(db_conn, "w1", x=100.0, y=100.0, vx=600.0, vy=0.0)
    tick = WorldTick()
    tick._last_flush_at = time.monotonic()
    assert tick.step() == 1
    assert len(persistence.dirty) == 1
    db_pos, _, _ = _db_state("w1")
    assert db_pos == [100.0, 100.0]
    through_pos, _, _ = _through_state("w1")
    assert through_pos[0] == pytest.approx(100.0 + 600.0 * TICK_DT)
    assert through_pos[1] == pytest.approx(100.0)


def test_flush_on_time_threshold(db_conn):
    _insert_soul(db_conn, "w2", x=100.0, y=100.0, vx=600.0, vy=0.0)
    tick = WorldTick()
    tick._last_flush_at = 0.0
    tick.step()
    assert len(persistence.dirty) == 0
    db_pos, _, _ = _db_state("w2")
    assert db_pos[0] == pytest.approx(100.0 + 600.0 * TICK_DT)


def test_flush_on_size_threshold(db_conn, monkeypatch):
    monkeypatch.setattr(persistence, "FLUSH_MAX_DIRTY", 2)
    for i in range(3):
        _insert_soul(db_conn, f"ws{i}", x=100.0, y=100.0, vx=50.0, vy=0.0)
    tick = WorldTick()
    tick._last_flush_at = time.monotonic()
    tick.step()
    assert len(persistence.dirty) == 0
    for i in range(3):
        db_pos, _, _ = _db_state(f"ws{i}")
        assert db_pos[0] == pytest.approx(100.0 + 50.0 * TICK_DT)


def test_stop_flushes_everything(db_conn, monkeypatch):
    monkeypatch.setattr(persistence, "FLUSH_INTERVAL_S", 3600.0)
    _insert_soul(db_conn, "w3", x=100.0, y=100.0, vx=600.0, vy=0.0)
    tick = WorldTick()
    tick.step()
    assert len(persistence.dirty) == 1

    async def go():
        await tick.stop()

    import asyncio

    asyncio.run(go())
    assert len(persistence.dirty) == 0
    db_pos, _, _ = _db_state("w3")
    assert db_pos[0] == pytest.approx(100.0 + 600.0 * TICK_DT)


def test_adjudication_journals_atomically(db_conn):
    _insert_soul(db_conn, "j1", x=100.0, y=100.0)
    record = intents.enqueue_intent(
        "sessJ", "nJ", None, "j1", "move_to", {"x": 400.0, "y": 100.0}
    )
    WorldTick().pump_intents()
    with database.get_db() as conn:
        status = conn.execute(
            "SELECT status FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()["status"]
    assert status == "adjudicated"
    rows = _journal_rows(record["intent_id"])
    assert len(rows) == 1
    assert rows[0]["type"] == persistence.EVENT_INTENT_ADJUDICATED
    payload = json.loads(rows[0]["payload"])
    assert payload["soul_id"] == "j1"
    assert payload["velocity"][0] == pytest.approx(INTENT_MOVE_SPEED)
    assert payload["move_target"] == [400.0, 100.0]
    _, db_vel, db_target = _db_state("j1")
    assert db_vel[0] == pytest.approx(INTENT_MOVE_SPEED)
    assert db_target == [400.0, 100.0]


def test_journal_ignores_position_integration(db_conn):
    _insert_soul(db_conn, "j2", x=100.0, y=100.0, vx=600.0, vy=0.0)
    tick = WorldTick()
    tick._last_flush_at = time.monotonic()
    for _ in range(3):
        tick.step()
    with database.get_db() as conn:
        head = persistence.journal_head(conn)
    assert head == 0


def test_journal_replay_is_idempotent():
    state = {"s9": {"position": [100.0, 100.0], "velocity": [0.0, 0.0],
                    "move_target": None}}
    event = {
        "seq": 1,
        "tick_id": 0,
        "type": persistence.EVENT_INTENT_ADJUDICATED,
        "payload": {
            "intent_id": "int_x",
            "kind": "move_to",
            "soul_id": "s9",
            "params": {"x": 400.0, "y": 100.0},
            "velocity": [600.0, 0.0],
            "move_target": [400.0, 100.0],
        },
        "created_at": 1.0,
    }
    persistence.apply_event(state, event)
    first = json.dumps(state["s9"], sort_keys=True)
    persistence.apply_event(state, event)
    assert json.dumps(state["s9"], sort_keys=True) == first
    assert state["s9"]["velocity"] == [600.0, 0.0]
    assert state["s9"]["move_target"] == [400.0, 100.0]


def test_snapshot_roundtrip_and_prune(db_conn):
    _insert_soul(db_conn, "s1", x=111.0, y=222.0, vx=5.0, vy=6.0)
    _insert_soul(db_conn, "s2", x=333.0, y=444.0)
    ids = []
    with database.get_db() as conn:
        for tick_id in range(5):
            ids.append(persistence.take_snapshot(conn, tick_id))
        assert persistence.prune_snapshots(conn) == 2
        remaining = persistence.latest_snapshots(conn, limit=10)
        assert len(remaining) == 3
        assert [r["snapshot_id"] for r in remaining] == ids[-1:-4:-1]
        snap = persistence.load_snapshot(conn, ids[-1])
    assert snap["tick_id"] == 4
    assert snap["souls"]["s1"]["position"] == [111.0, 222.0]
    assert snap["souls"]["s1"]["velocity"] == [5.0, 6.0]
    assert snap["souls"]["s2"]["move_target"] is None


def test_corrupt_snapshot_falls_back_to_older(db_conn):
    _insert_soul(db_conn, "c1", x=100.0, y=100.0)
    tick = WorldTick()
    with database.get_db() as conn:
        sid1 = persistence.take_snapshot(conn, 10)
        created = conn.execute(
            "SELECT created_at FROM snapshots WHERE snapshot_id = ?", (sid1,)
        ).fetchone()["created_at"]
        sid2 = persistence.take_snapshot(conn, 20)
        conn.execute(
            "UPDATE snapshots SET blob = ? WHERE snapshot_id = ?",
            (b"\x00\x01not-zlib", sid2),
        )
        conn.commit()
    report = persistence.recover_world(tick, now=created + 5.0)
    assert report["mode"] == "snapshot"
    assert report["snapshot_id"] == sid1
    assert report["regime"] == "fast_forward"
    assert tick.tick_id == 10 + int(round(5.0 / TICK_DT))


def test_journal_gap_falls_back_to_rebuild(db_conn):
    _insert_soul(db_conn, "g1", x=100.0, y=100.0)
    tick = WorldTick()
    with database.get_db() as conn:
        persistence.take_snapshot(conn, 0)
    a = intents.enqueue_intent("sg", "na", None, "g1", "move_to", {"x": 700.0, "y": 100.0})
    WorldTick().pump_intents()
    intents.enqueue_intent("sg", "nb", None, "g1", "move_to", {"x": 700.0, "y": 500.0})
    WorldTick().pump_intents()
    with database.get_db() as conn:
        conn.execute(
            "DELETE FROM journal WHERE payload LIKE ?",
            (f'%"intent_id": "{a["intent_id"]}"%',),
        )
        conn.commit()
    report = persistence.recover_world(tick)
    assert report["mode"] == "rebuild"
    _, db_vel, db_target = _db_state("g1")
    assert db_target == [700.0, 500.0]
    assert math.hypot(*db_vel) == pytest.approx(INTENT_MOVE_SPEED)


def test_recovery_rebuild_short_gap_fast_forward(db_conn):
    _insert_soul(db_conn, "r1", x=100.0, y=100.0, vx=600.0, vy=0.0,
                 target=[1000.0, 100.0])
    t0 = time.time()
    with database.get_db() as conn:
        persistence.flush_dirty(conn, 42)
    tick = WorldTick()
    report = persistence.recover_world(tick, now=t0 + 30.0)
    assert report["mode"] == "rebuild"
    assert report["regime"] == "fast_forward"
    assert report["gap_seconds"] == pytest.approx(30.0)
    assert tick.tick_id == 42 + int(round(30.0 / TICK_DT))
    pos, vel, target = (100.0, 100.0), (600.0, 0.0), (1000.0, 100.0)
    for _ in range(int(round(30.0 / TICK_DT))):
        pos, vel, target = persistence.integrate_soul(
            pos, vel, target, TICK_DT, database.SCREEN_BOUNDS
        )
    db_pos, db_vel, db_target = _db_state("r1")
    assert db_pos == [pytest.approx(pos[0]), pytest.approx(pos[1])]
    assert db_vel == [pytest.approx(vel[0]), pytest.approx(vel[1])]
    assert db_target == ([pytest.approx(target[0]), pytest.approx(target[1])]
                         if target else None)


def test_recovery_long_gap_analytic_capped_at_24h(db_conn):
    width, _ = database.SCREEN_BOUNDS
    _insert_soul(db_conn, "r2", x=100.0, y=100.0, vx=10.0, vy=0.0)
    t0 = time.time()
    with database.get_db() as conn:
        persistence.flush_dirty(conn, 7)
    tick48 = WorldTick()
    report48 = persistence.recover_world(tick48, now=t0 + 48 * 3600.0)
    assert report48["regime"] == "analytic"
    assert report48["sim_seconds"] == pytest.approx(24 * 3600.0)
    assert tick48.tick_id == 7 + int(round(24 * 3600.0 / TICK_DT))
    db_pos, _, _ = _db_state("r2")
    assert db_pos[0] == pytest.approx(min(100.0 + 10.0 * 86400.0, width - 10.0))
    tick24 = WorldTick()
    persistence.recover_world(tick24, now=t0 + 24 * 3600.0)
    db_pos24, _, _ = _db_state("r2")
    assert db_pos24 == db_pos


def test_recovery_snapshot_fast_forward_matches_live(db_conn):
    _insert_soul(db_conn, "d1", x=100.0, y=100.0)
    tick = WorldTick()
    with database.get_db() as conn:
        persistence.take_snapshot(conn, 0)
        created = conn.execute(
            "SELECT created_at FROM snapshots ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()["created_at"]
    intents.enqueue_intent("sd", "nd", None, "d1", "move_to", {"x": 400.0, "y": 100.0})
    tick._last_flush_at = time.monotonic()
    tick.step()
    tick.step()
    live_pos, _, _ = _through_state("d1")
    assert live_pos[0] == pytest.approx(340.0, abs=1e-9)
    assert live_pos[1] == pytest.approx(100.0, abs=1e-9)
    tick2 = WorldTick()
    report = persistence.recover_world(tick2, now=created + 0.4)
    assert report["mode"] == "snapshot"
    assert report["regime"] == "fast_forward"
    assert report["journal_replayed"] == 1
    assert tick2.tick_id == 2
    db_pos, _, _ = _db_state("d1")
    assert db_pos[0] == pytest.approx(live_pos[0], abs=1e-9)
    assert db_pos[1] == pytest.approx(live_pos[1], abs=1e-9)


def test_deterministic_replay_of_adjudicated_intents(db_conn):
    _insert_soul(db_conn, "n1", x=100.0, y=100.0)
    _insert_soul(db_conn, "n2", x=500.0, y=500.0)
    tick = WorldTick()
    with database.get_db() as conn:
        persistence.take_snapshot(conn, 0)
        snap = persistence.load_snapshot(
            conn, persistence.latest_snapshots(conn, limit=1)[0]["snapshot_id"]
        )
    outcomes = []
    intents.enqueue_intent("sn", "n1a", None, "n1", "move_to", {"x": 400.0, "y": 100.0})
    tick.step()
    intents.enqueue_intent("sn", "n2a", None, "n2", "move_to", {"x": 500.0, "y": 900.0})
    tick.step()
    tick.step()
    intents.enqueue_intent("sn", "n1b", None, "n1", "move_to", {"x": 900.0, "y": 900.0})
    tick.step()
    tick.step()
    live = {sid: _through_state(sid) for sid in ("n1", "n2")}
    with database.get_db() as conn:
        events = persistence.journal_tail(conn, snap["journal_seq"])
    outcomes = [(e["tick_id"], e["type"], e["payload"]) for e in events]
    assert len(outcomes) == 3
    state = {
        sid: {
            "position": list(e["position"]),
            "velocity": list(e["velocity"]),
            "move_target": (list(e["move_target"]) if e["move_target"] else None),
        }
        for sid, e in snap["souls"].items()
    }
    end_tick = persistence.replay_tail(
        state, events, snap["tick_id"], 5 * TICK_DT, TICK_DT,
        database.SCREEN_BOUNDS, exact=True,
    )
    assert end_tick == 5
    for sid in ("n1", "n2"):
        assert state[sid]["position"][0] == pytest.approx(live[sid][0][0], abs=1e-9)
        assert state[sid]["position"][1] == pytest.approx(live[sid][0][1], abs=1e-9)
        assert state[sid]["velocity"] == [
            pytest.approx(live[sid][1][0], abs=1e-9),
            pytest.approx(live[sid][1][1], abs=1e-9),
        ]
        assert state[sid]["move_target"] == live[sid][2]


def test_pending_intent_reconciled_on_boot(db_conn):
    _insert_soul(db_conn, "p1", x=100.0, y=100.0)
    record = intents.enqueue_intent(
        "sp", "np", None, "p1", "move_to", {"x": 800.0, "y": 100.0}
    )
    tick = WorldTick()
    report = persistence.recover_world(tick)
    assert report["mode"] == "rebuild"
    pumped = tick.pump_intents()
    assert pumped == 1
    with database.get_db() as conn:
        status = conn.execute(
            "SELECT status FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()["status"]
    assert status == "adjudicated"
    rows = _journal_rows(record["intent_id"])
    assert len(rows) == 1
    assert rows[0]["type"] == persistence.EVENT_INTENT_ADJUDICATED


def test_viewport_sees_unflushed_dirty(db_conn):
    _insert_soul(db_conn, "v1", x=100.0, y=200.0, vx=600.0, vy=0.0)
    tick = WorldTick()
    tick._last_flush_at = time.monotonic()
    tick.step()
    positions = viewport.read_positions()
    assert positions["v1"][0] == pytest.approx(100.0 + 600.0 * TICK_DT)
    assert positions["v1"][1] == pytest.approx(200.0)


def test_rest_read_through_and_upsert_invalidates(client, db_conn, register_soul):
    register_soul("rt1")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET position = ?, velocity = ? WHERE soul_id = ?",
            (json.dumps([100.0, 100.0]), json.dumps([600.0, 0.0]), "rt1"),
        )
        conn.commit()
    tick = WorldTick()
    tick._last_flush_at = time.monotonic()
    tick.step()
    res = client.get("/souls", params={"owner_id": "owner_rt1"})
    assert res.status_code == 200
    soul = {s["soul_id"]: s for s in res.json()}["rt1"]
    assert soul["position"][0] == pytest.approx(100.0 + 600.0 * TICK_DT)
    res = client.post(
        "/souls",
        json={"owner_id": "owner_rt1",
              "souls": [{"soul_id": "rt1", "position": [5.0, 5.0]}]},
    )
    assert res.status_code == 200
    assert persistence.dirty_get("rt1") is None
    res = client.get("/souls", params={"owner_id": "owner_rt1"})
    soul = {s["soul_id"]: s for s in res.json()}["rt1"]
    assert soul["position"] == [5.0, 5.0]


def test_reconcile_escrows_without_table(db_conn):
    assert persistence.reconcile_escrows(db_conn) == 0


def test_flush_updates_tick_watermarks(db_conn):
    _insert_soul(db_conn, "m1", x=100.0, y=100.0, vx=600.0, vy=0.0)
    tick = WorldTick()
    tick._last_flush_at = 0.0
    before = time.time()
    tick.step()
    with database.get_db() as conn:
        at, tick_id = persistence.last_tick_meta(conn)
    assert at is not None and at >= before
    assert tick_id == 1
