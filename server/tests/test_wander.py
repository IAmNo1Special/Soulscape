"""Baseline idle wander (no-LLM fallback drift).

An idle, healthy, funded soul should amble on its own so online
motion resembles the offline roam even when no brain fires.
"""

import json
import math
import random

import pytest

from .. import database
from .. import intents
from .. import wander
from ..agents import jev as jev_mod
from ..agents import pool as pool_mod
from ..world_tick import WorldTick


def _insert_soul(db_conn, soul_id, x=500.0, y=500.0, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "custodian_id": f"owner_{soul_id}",
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


class _Tick:
    """Minimal tick double: real pool, no JEV worker."""

    def __init__(self):
        self.tick_id = 7
        self.agent_pool = pool_mod.AgentPool()
        self._jev_worker = None
        self._baseline_wander_at = {}


def _pending_for(soul_id):
    return [
        i
        for i in intents.pending_intents()
        if i["soul_id"] == soul_id and i["kind"] == "move_to"
    ]


def test_idle_soul_wanders_at_amble_speed(db_conn):
    _insert_soul(db_conn, "s1")
    tick = _Tick()
    made = wander.sweep(tick, rng=random.Random(3), prob=1.0)
    assert made == 1
    pending = _pending_for("s1")
    assert len(pending) == 1
    payload = pending[0]["payload"]
    assert payload["pace"] == "amble" and payload["wander"] is True
    dist = math.hypot(payload["x"] - 500.0, payload["y"] - 500.0)
    assert wander.WANDER_MIN_DIST_U <= dist <= wander.WANDER_MAX_DIST_U + 1e-6

    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute("SELECT velocity FROM souls WHERE soul_id = 's1'").fetchone()
    vx, vy = json.loads(row["velocity"])
    assert math.hypot(vx, vy) == pytest.approx(jev_mod.JEV_AMBLE_SPEED)


def test_moving_dormant_collapsed_needy_souls_skipped(db_conn):
    _insert_soul(db_conn, "moving", velocity=json.dumps([10.0, 0.0]))
    _insert_soul(db_conn, "dormant", essence=0.0)
    _insert_soul(db_conn, "collapsed", state="collapsed")
    _insert_soul(db_conn, "starving", satiety=10.0)
    _insert_soul(db_conn, "parched", hydration=10.0)
    tick = _Tick()
    assert wander.sweep(tick, rng=random.Random(3), prob=1.0) == 0
    for sid in ("moving", "dormant", "collapsed", "starving", "parched"):
        assert _pending_for(sid) == []


def test_cooldown_and_cap_bound_output(db_conn):
    for i in range(6):
        _insert_soul(db_conn, f"c{i}", x=100.0 + i * 50.0, y=100.0)
    tick = _Tick()
    made = wander.sweep(tick, rng=random.Random(3), prob=1.0)
    assert made == wander.WANDER_MAX_PER_TICK
    # The capped remainder goes next sweep; then everyone cools down.
    assert wander.sweep(tick, rng=random.Random(3), prob=1.0) == 2
    assert wander.sweep(tick, rng=random.Random(3), prob=1.0) == 0


def test_prob_zero_wanders_nothing(db_conn):
    _insert_soul(db_conn, "s1")
    assert wander.sweep(_Tick(), rng=random.Random(3), prob=0.0) == 0
    assert _pending_for("s1") == []


def test_pick_wander_target_stays_in_bounds():
    rng = random.Random(11)
    for _ in range(50):
        tx, ty = wander.pick_wander_target(5.0, 5.0, rng, (1920.0, 1080.0))
        assert 10.0 <= tx <= 1910.0
        assert 10.0 <= ty <= 1070.0


def test_jev_owned_souls_are_left_alone(db_conn):
    _insert_soul(db_conn, "s1")

    class _Worker:
        def wandering(self, soul_id):
            return True

    tick = _Tick()
    tick._jev_worker = _Worker()
    assert wander.sweep(tick, rng=random.Random(3), prob=1.0) == 0
    assert _pending_for("s1") == []


def test_real_jev_worker_reports_wander_ownership():
    from ..agents.jev_queue import JevEventQueue
    from ..agents.jev_worker import JevWorker
    from ..agents.jev import JevBreaker

    worker = JevWorker(
        queue=JevEventQueue(),
        breaker=JevBreaker(),
        emit_fn=lambda *a: None,
        telemetry_fn=lambda *a: None,
    )
    assert worker.wandering("s1") is False
    worker._wander.start("s1", 960.0, 540.0)
    assert worker.wandering("s1") is True
    worker._wander.cancel("s1")
    worker._wander_intent["s1"] = "int_x"
    assert worker.wandering("s1") is True
