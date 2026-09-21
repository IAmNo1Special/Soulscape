import asyncio
import json
import math
import time

import pytest
from goapauto.testing import FakeTypeSafeClient

from .. import affection, database, intents
from ..agents import brain_busy
from ..agents import jev as jev_mod
from ..agents.jev_worker import JevWorker
from ..world_tick import WorldTick


@pytest.fixture(autouse=True)
def clean_busy():
    brain_busy.reset()
    yield
    brain_busy.reset()


def _enable(monkeypatch, key=True):
    monkeypatch.setenv("SOULSCAPE_JEV_TIER", "1")
    if key:
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    else:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def _disable(monkeypatch):
    monkeypatch.delenv("SOULSCAPE_JEV_TIER", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def _make_soul(db_conn, soul_id="s1"):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, name, essence, hp, max_hp,"
        " satiety, hydration, position, velocity, state)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            soul_id,
            "owner1",
            "S1",
            100.0,
            100.0,
            100.0,
            90.0,
            90.0,
            "[960.0, 540.0]",
            "[0.0, 0.0]",
            "normal",
        ),
    )
    db_conn.commit()


@pytest.fixture(autouse=True)
def scrub_proxy_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_jev_sensors(monkeypatch):
    real = jev_mod.SensorRegistry
    monkeypatch.setattr(
        jev_mod,
        "SensorRegistry",
        lambda client: real(
            FakeTypeSafeClient(answers={k: 0.0 for k in jev_mod.QUESTIONS})
        ),
    )


async def _wait_for(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def _fresh_db():
    return database.get_db()


def _count_move_to():
    with _fresh_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE soul_id = 's1'"
            " AND kind = 'move_to' AND status = 'adjudicated'"
        ).fetchone()["c"]


def test_disabled_by_default_starts_no_worker(monkeypatch):
    _disable(monkeypatch)

    async def go():
        tick = WorldTick(tick_dt=0.01)
        await tick.start()
        assert tick._jev_worker is None
        assert tick.agent_pool.jev_note is None
        await asyncio.sleep(0.05)
        await tick.stop()
        return tick

    tick = asyncio.run(go())
    assert tick.tick_id >= 1
    assert tick._jev_worker is None


def test_enabled_without_key_autodisables(monkeypatch):
    _enable(monkeypatch, key=False)

    async def go():
        tick = WorldTick(tick_dt=0.01)
        await tick.start()
        assert tick._jev_worker is None
        assert tick.agent_pool.jev_note is None
        await tick.stop()

    asyncio.run(go())


def test_enabled_starts_and_stops_worker(monkeypatch, fake_jev_sensors):
    _enable(monkeypatch)

    async def go():
        tick = WorldTick(tick_dt=0.01)
        await tick.start()
        assert isinstance(tick._jev_worker, JevWorker)
        assert tick._jev_worker._thread.is_alive()
        assert tick.agent_pool.jev_note is not None
        thread = tick._jev_worker._thread
        await tick.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert tick.agent_pool.jev_note is None
        assert tick._jev_worker is None

    asyncio.run(go())


def test_jev_note_before_init_is_noop():
    tick = WorldTick()
    tick._jev_note("ghost", "restlessness")


def test_end_to_end_restlessness_wanders_at_amble_speed(
    monkeypatch, fake_jev_sensors, db_conn
):
    _enable(monkeypatch)
    _make_soul(db_conn)

    async def go():
        tick = WorldTick(tick_dt=0.05)
        await tick.start()
        try:
            tick._jev_note("s1", "restlessness")
            ok = await _wait_for(lambda: _count_move_to() > 0, timeout=20.0)
            assert ok, "wander move_to was never adjudicated"
            with _fresh_db() as conn:
                row = conn.execute(
                    "SELECT payload, velocity FROM intents JOIN souls USING (soul_id)"
                    " WHERE intents.soul_id = 's1' AND intents.kind = 'move_to'"
                    " ORDER BY intents.rowid DESC LIMIT 1"
                ).fetchone()
            payload = json.loads(row["payload"])
            assert payload["pace"] == "amble"
            assert payload["wander"] is True
            vx, vy = json.loads(row["velocity"])
            assert math.isclose(
                math.hypot(vx, vy), jev_mod.JEV_AMBLE_SPEED, rel_tol=1e-6
            )
            with _fresh_db() as conn:
                usage = conn.execute(
                    "SELECT COUNT(*) AS c FROM jev_usage WHERE soul_id = 's1'"
                ).fetchone()["c"]
            assert usage >= 1
        finally:
            await tick.stop()

    asyncio.run(go())


def test_chirp_adjudication_notes_tamer_poke(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    tick = WorldTick()
    notes = []
    monkeypatch.setattr(
        tick,
        "_jev_note",
        lambda sid, kind, floor_override=None: notes.append((sid, kind)),
    )
    monkeypatch.setattr(
        affection,
        "adjudicate_affection_intent",
        lambda t, i: {"status": "adjudicated", "result": {"kind": "chirp"}},
    )
    tick._adjudicate_one(
        {
            "intent_id": "i1",
            "soul_id": "s1",
            "kind": "chirp",
            "custodian_id": "t1",
            "payload": {},
        }
    )
    assert notes == [("s1", "tamer_poke")]


def test_carry_move_does_not_note_jev(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    tick = WorldTick()
    notes = []
    monkeypatch.setattr(
        tick,
        "_jev_note",
        lambda sid, kind, floor_override=None: notes.append((sid, kind)),
    )
    monkeypatch.setattr(
        affection,
        "adjudicate_affection_intent",
        lambda t, i: {"status": "adjudicated", "result": {"phase": "move"}},
    )
    tick._adjudicate_one(
        {
            "intent_id": "i2",
            "soul_id": "s1",
            "kind": "carry_move",
            "custodian_id": "t1",
            "payload": {},
        }
    )
    assert notes == []


def test_look_intent_adjudicates(db_conn):
    _make_soul(db_conn)
    tick = WorldTick()
    intents.enqueue_intent("sess1", "nonce1", None, "s1", "look", {})
    done = tick.pump_intents()
    assert done == 1
    row = db_conn.execute(
        "SELECT status FROM intents WHERE kind = 'look' AND soul_id = 's1'"
    ).fetchone()
    assert row["status"] == "adjudicated"


def test_look_unknown_soul_rejects(db_conn):
    tick = WorldTick()
    intents.enqueue_intent("sess2", "nonce2", None, "ghost", "look", {})
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE kind = 'look' AND soul_id = 'ghost'"
    ).fetchone()
    assert row["status"] == "rejected"


def test_movement_completed_wander_chains_with_proven_target(
    monkeypatch, fake_jev_sensors, db_conn
):
    _enable(monkeypatch)
    _make_soul(db_conn)
    tx, ty = 877.2919808656147, 623.7412843344875
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO intents (intent_id, session_id, nonce, custodian_id,"
            " soul_id, kind, payload, status, created_at, result)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "mv1",
                "sess1",
                "n1",
                "owner1",
                "s1",
                "move_to",
                json.dumps({"x": tx, "y": ty, "pace": "amble", "wander": True}),
                "adjudicated",
                time.time(),
                json.dumps({"x": tx, "y": ty}),
            ),
        )
        conn.commit()

    async def go():
        tick = WorldTick()
        tick._jev_init()
        try:
            noted = []
            queue = tick._jev_queue
            real_note = queue.note
            queue.note = lambda sid, kind, now, snap, **kw: (
                noted.append((sid, kind)),
                real_note(sid, kind, now, snap, **kw),
            )[0]
            legs = []
            tick._jev_worker.on_wander_leg_complete = lambda sid, now, snap: (
                legs.append(sid)
            )
            tick._jev_worker._wander_intent["s1"] = "mv1"
            tick._jev_movement_completed([("s1", tx, ty)])
            assert ("s1", "movement_complete") in noted
            assert legs == ["s1"]
            noted.clear()
            legs.clear()
            tick._jev_movement_completed([("s1", tx + 50.0, ty)])
            assert ("s1", "movement_complete") in noted
            assert legs == []
        finally:
            tick._jev_shutdown()

    asyncio.run(go())


def test_movement_completed_non_wander_notes_only(
    monkeypatch, fake_jev_sensors, db_conn
):
    _enable(monkeypatch)
    _make_soul(db_conn)
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO intents (intent_id, session_id, nonce, custodian_id,"
            " soul_id, kind, payload, status, created_at, result)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "mv2",
                "sess2",
                "n2",
                "owner1",
                "s1",
                "move_to",
                json.dumps({"x": 100.0, "y": 100.0}),
                "adjudicated",
                time.time(),
                json.dumps({"x": 100.0, "y": 100.0}),
            ),
        )
        conn.commit()

    async def go():
        tick = WorldTick()
        tick._jev_init()
        try:
            noted = []
            queue = tick._jev_queue
            real_note = queue.note
            queue.note = lambda sid, kind, now, snap, **kw: (
                noted.append((sid, kind)),
                real_note(sid, kind, now, snap, **kw),
            )[0]
            legs = []
            tick._jev_worker.on_wander_leg_complete = lambda sid, now, snap: (
                legs.append(sid)
            )
            tick._jev_movement_completed([("s1", 100.0, 100.0)])
            assert ("s1", "movement_complete") in noted
            assert legs == []
        finally:
            tick._jev_shutdown()

    asyncio.run(go())


def test_wallet_delta_routes_jev_note(monkeypatch, fake_jev_sensors, db_conn):
    _enable(monkeypatch)
    _make_soul(db_conn)

    async def go():
        tick = WorldTick()
        tick._jev_init()
        try:
            assert tick.agent_pool.jev_note is not None
            noted = []
            queue = tick._jev_queue
            real_note = queue.note

            def spy_note(soul_id, kind, now, snapshot, **kwargs):
                noted.append((soul_id, kind))
                return real_note(soul_id, kind, now, snapshot, **kwargs)

            queue.note = spy_note
            now = time.time()
            await tick.agent_pool._think_inner("s1", tick.vision, None, 0, now)
            assert noted == []
            with database.get_db() as conn:
                conn.execute(
                    "UPDATE souls SET essence = essence + 50.0 WHERE soul_id = 's1'"
                )
                conn.commit()
            await tick.agent_pool._think_inner("s1", tick.vision, None, 0, now + 1)
            assert ("s1", "wallet_delta") in noted
        finally:
            tick._jev_shutdown()

    asyncio.run(go())


def _adjudicate_notes(monkeypatch, kind, outcome):
    tick = WorldTick()
    notes = []
    monkeypatch.setattr(
        tick,
        "_jev_note",
        lambda sid, k, floor_override=None: notes.append((sid, k)),
    )
    monkeypatch.setattr(affection, "adjudicate_affection_intent", lambda t, i: outcome)
    tick._adjudicate_one(
        {
            "intent_id": "i9",
            "soul_id": "s1",
            "kind": kind,
            "custodian_id": "t1",
            "payload": {},
        }
    )
    return notes


def test_chirp_rejected_no_tamer_poke(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    notes = _adjudicate_notes(
        monkeypatch, "chirp", {"status": "rejected", "result": None}
    )
    assert notes == []


def test_carry_grab_notes_carry_change(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    notes = _adjudicate_notes(
        monkeypatch,
        "carry_move",
        {"status": "adjudicated", "result": {"phase": "grab"}},
    )
    assert notes == [("s1", "carry_change")]


def test_carry_release_notes_carry_change(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    notes = _adjudicate_notes(
        monkeypatch,
        "carry_move",
        {
            "status": "adjudicated",
            "result": {"phase": "release", "was_carried": True},
        },
    )
    assert notes == [("s1", "carry_change")]


def test_carry_release_not_carried_no_note(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    notes = _adjudicate_notes(
        monkeypatch,
        "carry_move",
        {
            "status": "adjudicated",
            "result": {"phase": "release", "was_carried": False},
        },
    )
    assert notes == []


def test_carry_escape_notes_carry_change(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    notes = _adjudicate_notes(
        monkeypatch,
        "carry_move",
        {
            "status": "adjudicated",
            "result": {"phase": "move", "escaped": True},
        },
    )
    assert notes == [("s1", "carry_change")]


def test_vision_exit_notes_jev(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    tick = WorldTick()
    notes = []
    monkeypatch.setattr(
        tick,
        "_jev_note",
        lambda sid, kind, floor_override=None: notes.append((sid, kind)),
    )
    tick._note_vision_events(
        {"s1": {"entered": [], "exited": [{"id": "s2", "kind": "soul"}]}},
        time.time(),
    )
    assert notes == [("s1", "vision_exit")]


def test_step_performs_no_network_calls(monkeypatch, db_conn):
    import socket
    import threading

    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    calls = []
    real_connect = socket.socket.connect

    def spy_connect(self, address):
        calls.append((threading.current_thread().name, address))
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    tick = WorldTick()
    tick._jev_init()
    try:
        main_thread = threading.current_thread().name
        tick._jev_note("s1", "tamer_poke")
        tick.step()
        main_calls = [c for c in calls if c[0] == main_thread]
        assert main_calls == []
    finally:
        tick._jev_shutdown()


def _enqueue_intent(db_conn, intent_id, soul_id, kind):
    db_conn.execute(
        "INSERT INTO intents (intent_id, session_id, nonce, soul_id, kind,"
        " status, payload, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (intent_id, "sess1", intent_id, soul_id, kind, "pending", "{}", 1000.0),
    )
    db_conn.commit()


def test_rest_adjudication_stops_and_rests(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    _enqueue_intent(db_conn, "i-rest", "s1", "rest")
    tick = WorldTick()
    tick._adjudicate_one(
        {
            "intent_id": "i-rest",
            "soul_id": "s1",
            "kind": "rest",
            "custodian_id": "t1",
            "payload": {},
        }
    )
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = 'i-rest'"
    ).fetchone()
    assert row["status"] == "adjudicated"
    soul = db_conn.execute("SELECT activity FROM souls WHERE soul_id = 's1'").fetchone()
    assert soul["activity"] == "rest"


def test_wait_adjudication_stops(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    _enqueue_intent(db_conn, "i-wait", "s1", "wait")
    tick = WorldTick()
    tick._adjudicate_one(
        {
            "intent_id": "i-wait",
            "soul_id": "s1",
            "kind": "wait",
            "custodian_id": "t1",
            "payload": {},
        }
    )
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = 'i-wait'"
    ).fetchone()
    assert row["status"] == "adjudicated"


def test_movement_complete_snapshot_uses_arrived_position(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    tick = WorldTick()
    tick._jev_init()
    try:
        noted = []

        def spy_note(soul_id, kind, now, snapshot, **kwargs):
            noted.append((soul_id, kind, snapshot))

        monkeypatch.setattr(tick._jev_worker, "note", spy_note)
        tick._jev_movement_completed([("s1", 400.0, 100.0)])
        assert len(noted) == 1
        _, kind, snapshot = noted[0]
        assert kind == "movement_complete"
        assert snapshot["position"] == [400.0, 100.0]
    finally:
        tick._jev_shutdown()


def test_wander_completion_binds_exact_intent(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    db_conn.execute(
        "INSERT INTO intents (intent_id, session_id, nonce, soul_id, kind,"
        " status, payload, created_at, result)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "wander-i1",
            "sess1",
            "wander-i1",
            "s1",
            "move_to",
            "adjudicated",
            '{"x": 400.0, "y": 100.0, "wander": true}',
            1000.0,
            '{"x": 400.0, "y": 100.0}',
        ),
    )
    db_conn.commit()
    tick = WorldTick()
    tick._jev_init()
    try:
        continued = []
        monkeypatch.setattr(
            tick._jev_worker,
            "on_wander_leg_complete",
            lambda sid, now, snap: continued.append(sid),
        )
        tick._jev_worker._wander_intent["s1"] = "wander-i1"
        tick._jev_movement_completed([("s1", 400.0, 100.0)])
        assert continued == ["s1"]
    finally:
        tick._jev_shutdown()


def test_wander_completion_ignores_non_wander_intent(monkeypatch, db_conn):
    _enable(monkeypatch, key=True)
    _make_soul(db_conn)
    db_conn.execute(
        "INSERT INTO intents (intent_id, session_id, nonce, soul_id, kind,"
        " status, payload, created_at, result)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "move-i1",
            "sess1",
            "move-i1",
            "s1",
            "move_to",
            "adjudicated",
            '{"x": 400.0, "y": 100.0}',
            1000.0,
            '{"x": 400.0, "y": 100.0}',
        ),
    )
    db_conn.commit()
    tick = WorldTick()
    tick._jev_init()
    try:
        continued = []
        monkeypatch.setattr(
            tick._jev_worker,
            "on_wander_leg_complete",
            lambda sid, now, snap: continued.append(sid),
        )
        tick._jev_worker._wander_intent["s1"] = "move-i1"
        tick._jev_movement_completed([("s1", 400.0, 100.0)])
        assert continued == []
    finally:
        tick._jev_shutdown()
