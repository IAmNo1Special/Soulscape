"""Tests for issue #21: collapsed state machine, biology decay, feed_soul.

Every acceptance criterion gets real evidence:
- decay rates over simulated days (closed-form, accelerated -- no wall clock)
- starvation as the ONLY collapse route (single writer, functional tests)
- recovery gated on fed_flag AND >= 6 h continuous rest
- feed_soul atomicity (one commit), idempotent nonce, crash-window behavior
- statue state streamed through the viewport snapshot/delta path
"""

import json
import time

import pytest

from shared import protocol

from .. import biology
from .. import database
from .. import intents
from .. import persistence
from .. import viewport
from ..world_tick import INTENT_MOVE_SPEED, WorldTick


def _fields(**over):
    base = {
        "soul_id": "s",
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "state": "normal",
        "fed_flag": 0,
        "rest_started_at": None,
        "activity": None,
        "velocity": (0.0, 0.0),
        "move_target": None,
    }
    base.update(over)
    return base


def _insert_soul(db_conn, soul_id, **over):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "essence": 100.0,
        "state": "normal",
        "fed_flag": 0,
        "rest_started_at": None,
        "activity": None,
        "position": json.dumps([100.0, 100.0]),
        "velocity": json.dumps([0.0, 0.0]),
        "move_target": None,
    }
    cols.update(over)
    db_conn.execute(
        f"INSERT INTO souls ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )
    db_conn.commit()


def _row(db_conn, soul_id):
    return dict(
        db_conn.execute("SELECT * FROM souls WHERE soul_id = ?", (soul_id,)).fetchone()
    )


def _journal_types(db_conn):
    return [
        r[0]
        for r in db_conn.execute("SELECT type FROM journal ORDER BY seq").fetchall()
    ]


# ---------------------------------------------------------------------------
# Decay rates over simulated days
# ---------------------------------------------------------------------------


def test_satiety_resting_36h():
    now = time.time()
    f = _fields()
    assert biology._decay_soul(f, 18 * 3600, now)["satiety"] == pytest.approx(50.0)
    assert biology._decay_soul(f, 28.8 * 3600, now)["satiety"] == pytest.approx(20.0)
    assert biology._decay_soul(f, 36 * 3600, now)["satiety"] == pytest.approx(0.0)
    # Floors at zero, never negative.
    assert biology._decay_soul(f, 72 * 3600, now)["satiety"] == pytest.approx(0.0)


def test_satiety_active_18h():
    now = time.time()
    f = _fields(velocity=(300.0, 0.0))
    assert biology._decay_soul(f, 9 * 3600, now)["satiety"] == pytest.approx(50.0)
    assert biology._decay_soul(f, 18 * 3600, now)["satiety"] == pytest.approx(0.0)


def test_satiety_active_via_move_target():
    now = time.time()
    f = _fields(move_target=(500.0, 500.0))
    assert biology._decay_soul(f, 18 * 3600, now)["satiety"] == pytest.approx(0.0)


def test_hydration_24h_activity_independent():
    now = time.time()
    resting = _fields()
    active = _fields(velocity=(600.0, 0.0))
    assert biology._decay_soul(resting, 24 * 3600, now)["hydration"] == pytest.approx(
        0.0
    )
    assert biology._decay_soul(active, 24 * 3600, now)["hydration"] == pytest.approx(
        0.0
    )
    assert biology._decay_soul(resting, 12 * 3600, now)["hydration"] == pytest.approx(
        50.0
    )


def test_hp_chip_only_while_starving():
    now = time.time()
    # Satiety 30 over 3 h: stays above 20 -> no chip.
    f = _fields(satiety=30.0, hp=100.0)
    out = biology._decay_soul(f, 3 * 3600, now)
    assert out["satiety"] == pytest.approx(30.0 - 3 * 100.0 / 36.0)
    assert "hp" not in out
    # Satiety 19: starving -> -1/h.
    f = _fields(satiety=19.0, hp=100.0)
    out = biology._decay_soul(f, 5 * 3600, now)
    assert out["hp"] == pytest.approx(95.0)
    # Crossing the threshold mid-span chips only the starving tail:
    # satiety 30 resting -> 20 after 3.6 h, so 1.4 h of chip over 5 h.
    f = _fields(satiety=30.0, hp=100.0)
    out = biology._decay_soul(f, 5 * 3600, now)
    assert out["hp"] == pytest.approx(100.0 - 1.4, abs=0.01)


def test_starvation_collapse_timing():
    # Resting from full: starving at 28.8 h, then 100 h of -1/h chip.
    now = time.time()
    elapsed = 0.0
    f = _fields()
    while True:
        out = biology._decay_soul(f, 3600, now + elapsed)
        elapsed += 3600
        for k, v in out.items():
            if not k.startswith("_"):
                f[k] = v
        if out.get("_collapsed"):
            break
        assert elapsed < 200 * 3600, "never collapsed"
    assert elapsed == pytest.approx(128.8 * 3600, rel=0.01)
    assert f["state"] == "collapsed"
    assert f["hp"] == 0.0


def test_collapse_journals_and_zeros_velocity(db_conn):
    _insert_soul(
        db_conn,
        "c1",
        satiety=10.0,
        hp=2.0,
        velocity=json.dumps([120.0, 0.0]),
        move_target=json.dumps([500.0, 500.0]),
    )
    now = time.time()
    with database.get_db() as conn:
        report = biology.apply_biology_decay(conn, 3 * 3600, now, tick_id=7)
        conn.commit()
    assert report["collapsed"] == 1
    row = _row(db_conn, "c1")
    assert row["state"] == "collapsed"
    assert row["hp"] == 0.0
    assert json.loads(row["velocity"]) == [0.0, 0.0]
    assert row["move_target"] is None
    assert row["rest_started_at"] == pytest.approx(now - 3600, abs=60)
    assert persistence.EVENT_SOUL_COLLAPSED in _journal_types(db_conn)


def test_biology_cadence_one_advance_per_50_steps(db_conn, monkeypatch):
    # The 0.1 Hz tick fires after every 50th step -- never on the first
    # step, exactly once per 50 steps.
    calls = []
    real = biology.apply_biology_tick

    def spy(conn, tick_id, now, tick_dt):
        calls.append(tick_id)
        return real(conn, tick_id, now, tick_dt)

    monkeypatch.setattr(biology, "apply_biology_tick", spy)
    tick = WorldTick()
    for _ in range(100):
        tick.step()
    assert calls == [49, 99]


def test_live_tick_and_catchup_agree(db_conn):
    # Live biology ticks and one closed-form catch-up cover the same 600 s.
    _insert_soul(db_conn, "live", velocity=json.dumps([50.0, 0.0]))
    _insert_soul(db_conn, "catch", velocity=json.dumps([50.0, 0.0]))
    now = time.time()
    tick = WorldTick()
    span = biology.BIOLOGY_EVERY_TICKS * tick.tick_dt
    with database.get_db() as conn:
        t = now
        for i in range(360):
            biology.apply_biology_tick(conn, (i + 1) * 50 - 1, t, tick.tick_dt)
            t += span
    with database.get_db() as conn:
        biology.apply_biology_decay(conn, 360 * span, now, tick_id=999)
        conn.commit()
    live = _row(db_conn, "live")
    catch = _row(db_conn, "catch")
    for col in ("satiety", "hydration", "hp"):
        assert live[col] == pytest.approx(catch[col], abs=1e-9), col


def test_catchup_capped_at_24h(db_conn):
    _insert_soul(db_conn, "cap")
    now = time.time()
    with database.get_db() as conn:
        biology.apply_biology_decay(conn, 48 * 3600, now, tick_id=1)
        conn.commit()
    row = _row(db_conn, "cap")
    # 24 h resting: satiety 100 - 24*(100/36) = 33.33, hydration 0.
    assert row["satiety"] == pytest.approx(100.0 - 24 * 100.0 / 36.0, abs=1e-6)
    assert row["hydration"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Only starvation collapses
# ---------------------------------------------------------------------------


def test_only_biology_writes_collapsed():
    import os

    server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    writers = []
    for name in os.listdir(server_dir):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        path = os.path.join(server_dir, name)
        with open(path) as fh:
            for lineno, line in enumerate(fh, 1):
                if "collapsed" in line and (
                    "SET state" in line
                    or '="collapsed"' in line
                    or "='collapsed'" in line
                    or '["state"]' in line
                ):
                    writers.append((name, lineno, line.strip()))
    assert writers, "expected at least the biology writer"
    assert all(name == "biology.py" for name, _, _ in writers), writers


def test_state_check_constraint_rejects_garbage(db_conn):
    _insert_soul(db_conn, "chk")
    with pytest.raises(Exception):
        db_conn.execute("UPDATE souls SET state = 'ghost' WHERE soul_id = 'chk'")


def test_feed_cannot_collapse_and_move_rejected_when_collapsed(db_conn):
    _insert_soul(db_conn, "feeder")
    _insert_soul(db_conn, "victim")
    tick = WorldTick()
    record, _ = biology.enqueue_feed_soul(
        "s",
        "n1",
        None,
        "feeder",
        "feed_soul",
        {"feeder_soul_id": "feeder", "recipient_soul_id": "victim"},
    )
    tick.pump_intents()
    fresh = intents.get_intent_by_nonce("s", "n1")
    assert fresh["status"] == "rejected"
    assert fresh["result"]["reason"] == "not_collapsed"
    assert _row(db_conn, "victim")["state"] == "normal"
    assert _row(db_conn, "feeder")["state"] == "normal"
    # A collapsed soul cannot be moved either.
    db_conn.execute("UPDATE souls SET state = 'collapsed' WHERE soul_id = 'victim'")
    db_conn.commit()
    intents.enqueue_intent(
        "s", "n2", None, "victim", "move_to", {"x": 400.0, "y": 400.0}
    )
    tick.pump_intents()
    fresh = intents.get_intent_by_nonce("s", "n2")
    assert fresh["status"] == "rejected"
    assert fresh["result"]["reason"] == "collapsed"


# ---------------------------------------------------------------------------
# Recovery gating: fed AND rested >= 6 h
# ---------------------------------------------------------------------------


def test_recovery_needs_fed_and_rested():
    now = time.time()
    # Fed but only 1 h rested -> stays collapsed.
    f = _fields(state="collapsed", hp=0.0, fed_flag=1, rest_started_at=now - 3600)
    out = biology._decay_soul(f, 60, now)
    assert out.get("state", "collapsed") == "collapsed"
    # Rested 7 h but unfed -> stays collapsed.
    f = _fields(state="collapsed", hp=0.0, fed_flag=0, rest_started_at=now - 7 * 3600)
    out = biology._decay_soul(f, 60, now)
    assert out.get("state", "collapsed") == "collapsed"
    # Fed + rested 7 h -> recovers, flag cleared, journal written.
    f = _fields(state="collapsed", hp=0.0, fed_flag=1, rest_started_at=now - 7 * 3600)
    out = biology._decay_soul(f, 60, now)
    assert out["state"] == "normal"
    assert out["fed_flag"] == 0
    assert out["rest_started_at"] is None
    assert any(e == persistence.EVENT_SOUL_RECOVERED for e, _ in out["_journal"])


def test_collapse_starts_rest_clock(db_conn):
    _insert_soul(db_conn, "rc", satiety=5.0, hp=1.0)
    now = time.time()
    with database.get_db() as conn:
        biology.apply_biology_decay(conn, 2 * 3600, now, tick_id=3)
        conn.commit()
    row = _row(db_conn, "rc")
    assert row["state"] == "collapsed"
    # Collapse happened 1 h into the 2 h span; rest clock starts there.
    assert row["rest_started_at"] == pytest.approx(now - 3600, abs=120)


def test_rest_clock_resets_when_active():
    now = time.time()
    f = _fields(activity="rest", rest_started_at=now - 3600)
    out = biology._decay_soul(f, 60, now)
    assert "rest_started_at" not in out  # still resting, clock kept
    f = _fields(activity="rest", rest_started_at=now - 3600)
    f["activity"] = "foraging"
    out = biology._decay_soul(f, 60, now)
    assert out["rest_started_at"] is None  # active -> clock cleared


# ---------------------------------------------------------------------------
# feed_soul: atomic adjudication, idempotency, crash window, rejections
# ---------------------------------------------------------------------------


def _feed_setup(db_conn, feeder_essence=100.0):
    _insert_soul(db_conn, "feeder", essence=feeder_essence)
    _insert_soul(db_conn, "recip", essence=20.0, state="collapsed", hp=0.0)


def test_feed_soul_atomic_happy_path(db_conn):
    _feed_setup(db_conn)
    record, created = biology.enqueue_feed_soul(
        "sess",
        "nonce1",
        None,
        "feeder",
        "feed_soul",
        {"feeder_soul_id": "feeder", "recipient_soul_id": "recip"},
    )
    assert created is True
    # Commit-before-ack: the 10-essence hold is durable at enqueue.
    assert _row(db_conn, "feeder")["essence"] == pytest.approx(90.0)
    escrow = db_conn.execute(
        "SELECT status, amount FROM escrows WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert escrow["status"] == "held" and escrow["amount"] == 10.0

    WorldTick().pump_intents()

    fresh = intents.get_intent_by_nonce("sess", "nonce1")
    assert fresh["status"] == "adjudicated"
    # One atomic commit: flag + gift + ledger + escrow + journal.
    assert _row(db_conn, "recip")["fed_flag"] == 1
    assert _row(db_conn, "recip")["essence"] == pytest.approx(30.0)
    assert _row(db_conn, "feeder")["essence"] == pytest.approx(90.0)
    ledger = db_conn.execute(
        "SELECT entry_type, soul_id, amount FROM ledger WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchall()
    assert {(r["entry_type"], r["soul_id"], r["amount"]) for r in ledger} == {
        ("feed_debit", "feeder", -10.0),
        ("feed_credit", "recip", 10.0),
    }
    escrow = db_conn.execute(
        "SELECT status FROM escrows WHERE intent_id = ?", (record["intent_id"],)
    ).fetchone()
    assert escrow["status"] == "applied"
    assert persistence.EVENT_SOUL_FED in _journal_types(db_conn)


def test_feed_soul_crash_window_settles_exactly_once(db_conn):
    _feed_setup(db_conn)
    record, _ = biology.enqueue_feed_soul(
        "sess",
        "nonce1",
        None,
        "feeder",
        "feed_soul",
        {"feeder_soul_id": "feeder", "recipient_soul_id": "recip"},
    )
    # Simulate kill -9 between ack and adjudication: pending intent + held
    # escrow on disk. Boot reconcile must leave it alone...
    from .. import persistence as p

    with database.get_db() as conn:
        assert p.reconcile_escrows(conn) == 0
    assert intents.get_intent_by_nonce("sess", "nonce1")["status"] == "pending"
    # ...then the pump settles it exactly once...
    WorldTick().pump_intents()
    assert intents.get_intent_by_nonce("sess", "nonce1")["status"] == "adjudicated"
    # ...and a second pump is a no-op (no double gift, no double ledger).
    WorldTick().pump_intents()
    assert _row(db_conn, "recip")["essence"] == pytest.approx(30.0)
    assert _row(db_conn, "recip")["fed_flag"] == 1
    count = db_conn.execute(
        "SELECT COUNT(*) c FROM ledger WHERE intent_id = ?", (record["intent_id"],)
    ).fetchone()["c"]
    assert count == 2


def test_feed_soul_nonce_idempotent(db_conn):
    _feed_setup(db_conn)
    payload = {"feeder_soul_id": "feeder", "recipient_soul_id": "recip"}
    r1, c1 = biology.enqueue_feed_soul(
        "sess", "n", None, "feeder", "feed_soul", payload
    )
    r2, c2 = biology.enqueue_feed_soul(
        "sess", "n", None, "feeder", "feed_soul", payload
    )
    assert (c1, c2) == (True, False)
    assert r1["intent_id"] == r2["intent_id"]
    assert _row(db_conn, "feeder")["essence"] == pytest.approx(90.0)  # one hold


def test_feed_soul_rejections_release_escrow(db_conn):
    _feed_setup(db_conn)
    tick = WorldTick()

    def attempt(nonce, recipient, feeder="feeder"):
        record, _ = biology.enqueue_feed_soul(
            "sess",
            nonce,
            None,
            feeder,
            "feed_soul",
            {"feeder_soul_id": feeder, "recipient_soul_id": recipient},
        )
        tick.pump_intents()
        return intents.get_intent_by_nonce("sess", nonce)

    # Recipient not collapsed.
    _insert_soul(db_conn, "healthy")
    fresh = attempt("r1", "healthy")
    assert (
        fresh["status"] == "rejected" and fresh["result"]["reason"] == "not_collapsed"
    )
    # Missing recipient.
    fresh = attempt("r2", "ghost")
    assert fresh["result"]["reason"] == "recipient_not_found"
    # Already fed.
    db_conn.execute("UPDATE souls SET fed_flag = 1 WHERE soul_id = 'recip'")
    db_conn.commit()
    fresh = attempt("r3", "recip")
    assert fresh["result"]["reason"] == "already_fed"
    # Every rejection refunds the held 10 essence.
    assert _row(db_conn, "feeder")["essence"] == pytest.approx(100.0)
    assert _row(db_conn, "recip")["fed_flag"] == 1  # unchanged by rejects


def test_feed_soul_preface_refusals(db_conn):
    _insert_soul(db_conn, "poor", essence=5.0)
    _insert_soul(db_conn, "recip", state="collapsed", hp=0.0)
    payload = {"feeder_soul_id": "poor", "recipient_soul_id": "recip"}
    with pytest.raises(biology.BiologyRefusal) as exc:
        biology.enqueue_feed_soul("sess", "n", None, "poor", "feed_soul", payload)
    assert exc.value.reason == "insufficient_funds"
    assert intents.get_intent_by_nonce("sess", "n") is None  # no row written
    # A collapsed soul cannot feed.
    _insert_soul(db_conn, "flat", state="collapsed", hp=0.0)
    payload = {"feeder_soul_id": "flat", "recipient_soul_id": "recip"}
    with pytest.raises(biology.BiologyRefusal) as exc:
        biology.enqueue_feed_soul("sess", "n2", None, "flat", "feed_soul", payload)
    assert exc.value.reason == "feeder_collapsed"
    # Custody: a non-custodian cannot feed as someone else's soul.
    payload = {"feeder_soul_id": "poor", "recipient_soul_id": "recip"}
    with pytest.raises(biology.BiologyRefusal) as exc:
        biology.enqueue_feed_soul(
            "sess", "n3", "someone_else", "poor", "feed_soul", payload
        )
    assert exc.value.reason == "custody"


def test_feed_soul_rest_endpoint(client, db_conn, register_soul):
    register_soul("feeder1", essence=100.0)
    register_soul("recip1", essence=20.0)
    db_conn.execute(
        "UPDATE souls SET state = 'collapsed', hp = 0.0 WHERE soul_id = 'recip1'"
    )
    db_conn.commit()
    res = client.post(
        "/souls/feed",
        json={"feeder_soul_id": "feeder1", "recipient_soul_id": "recip1"},
        headers={"Idempotency-Key": "feed-key-1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success" and body["gift"] == 10.0
    assert _row(db_conn, "recip1")["fed_flag"] == 1
    # Idempotent retry returns the original outcome, no double gift.
    res2 = client.post(
        "/souls/feed",
        json={"feeder_soul_id": "feeder1", "recipient_soul_id": "recip1"},
        headers={"Idempotency-Key": "feed-key-1"},
    )
    assert res2.status_code == 200
    assert _row(db_conn, "feeder1")["essence"] == pytest.approx(90.0)
    # Feeding a healthy soul -> 400.
    register_soul("healthy1")
    res3 = client.post(
        "/souls/feed",
        json={"feeder_soul_id": "feeder1", "recipient_soul_id": "healthy1"},
    )
    assert res3.status_code == 400


def test_feed_soul_validator():
    payload, error = intents.validate_payload(
        "feed_soul",
        {"feeder_soul_id": "a", "recipient_soul_id": "b"},
    )
    assert error is None and payload["recipient_soul_id"] == "b"
    _, error = intents.validate_payload("feed_soul", {"feeder_soul_id": "a"})
    assert error == "BAD_PAYLOAD"


# ---------------------------------------------------------------------------
# Hungry speed penalty + statue stream
# ---------------------------------------------------------------------------


def test_hungry_speed_penalty_on_move(db_conn):
    _insert_soul(db_conn, "hungry", satiety=40.0)
    _insert_soul(db_conn, "full", satiety=60.0)
    tick = WorldTick()
    intents.enqueue_intent(
        "s", "m1", None, "hungry", "move_to", {"x": 900.0, "y": 100.0}
    )
    intents.enqueue_intent("s", "m2", None, "full", "move_to", {"x": 900.0, "y": 100.0})
    tick.pump_intents()
    f1 = intents.get_intent_by_nonce("s", "m1")
    f2 = intents.get_intent_by_nonce("s", "m2")
    assert f1["result"]["speed"] == pytest.approx(INTENT_MOVE_SPEED * 0.75)
    assert f2["result"]["speed"] == pytest.approx(INTENT_MOVE_SPEED)


def test_hungry_rescale_mid_journey(db_conn):
    # Soul moving at full speed becomes hungry mid-journey: the 0.1 Hz tick
    # clamps in-flight velocity to 75%, preserving direction.
    _insert_soul(db_conn, "runner", satiety=51.0, velocity=json.dumps([600.0, 0.0]))
    now = time.time()
    with database.get_db() as conn:
        # 1 h of ticks (50 x 72 s): satiety 51 -> ~48.2 (hungry) -> rescale.
        # apply_biology_tick itself is ungated (WorldTick.step owns the
        # every-50th-step cadence); the tick_id is only journal metadata.
        report = biology.apply_biology_tick(conn, 49, now + 3600, 72.0)
        conn.commit()
    assert report["rescaled"] == 1
    row = _row(db_conn, "runner")
    vx, vy = json.loads(row["velocity"])
    assert (vx**2 + vy**2) ** 0.5 == pytest.approx(450.0, abs=0.01)
    assert vy == pytest.approx(0.0)


def test_viewport_snapshot_carries_state(db_conn):
    _insert_soul(db_conn, "statue1", state="collapsed", hp=0.0)
    _insert_soul(db_conn, "walker1")
    session = viewport.ViewportSession(owner_id="o1")
    frame = viewport.build_snapshot(session, protocol.SnapReason.FULL_SYNC, 1)
    by_id = {s["soul_id"]: s for s in frame["souls"]}
    assert by_id["statue1"]["state"] == "collapsed"
    assert by_id["walker1"]["state"] == "normal"
    assert session.committed_states == {
        "statue1": "collapsed",
        "walker1": "normal",
    }


def test_viewport_diff_states_streams_transitions():
    ops = viewport.diff_states(
        {"a": "normal", "b": "collapsed"}, {"a": "normal", "b": "normal"}
    )
    assert len(ops) == 1
    op, domain = ops[0]
    assert op["state"] == {"soul_id": "b", "state": "collapsed"}
    assert op["op"] == protocol.EntityOpKind.UPSERT.value
    # No-op when nothing changed.
    assert viewport.diff_states({"a": "normal"}, {"a": "normal"}) == []


def test_schema_columns_and_defaults(db_conn):
    cols = {r[1]: r for r in db_conn.execute("PRAGMA table_info(souls)").fetchall()}
    assert cols["state"][4] == "'normal'"  # default
    assert cols["fed_flag"][4] == "0"
    _insert_soul(db_conn, "dflt")
    row = _row(db_conn, "dflt")
    assert row["state"] == "normal"
    assert row["fed_flag"] == 0
    assert row["rest_started_at"] is None
