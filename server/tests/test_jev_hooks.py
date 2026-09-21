import asyncio
import json
import time

import pytest

from .. import presence
from ..agents import brain_busy, deliberation, pool as pool_mod, reflex, scheduler


@pytest.fixture(autouse=True)
def _reset_singletons():
    brain_busy.reset()
    deliberation.reset_tracker()
    yield
    brain_busy.reset()
    deliberation.reset_tracker()


def _insert_soul(db_conn, soul_id, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "position": json.dumps([100.0, 100.0]),
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


def _think_pool():
    return pool_mod.AgentPool(think_scheduler=scheduler.ThinkScheduler(seed=9))


class _Vision:
    def detail_observations(self, soul_id):
        return []


def _think(p, soul_id, now):
    return asyncio.run(p.think(soul_id, _Vision(), reflex.NullProvider(), 0, now))


def test_needs_band_crossing_notes_jev(db_conn, monkeypatch):
    _insert_soul(db_conn, "s1", satiety=90.0)
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    _think(p, "s1", now)
    assert notes == []
    db_conn.execute("UPDATE souls SET satiety = 30.0 WHERE soul_id = 's1'")
    db_conn.commit()
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert ("s1", "needs_change") in notes


def test_needs_same_band_no_note(db_conn):
    _insert_soul(db_conn, "s1", satiety=90.0)
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    _think(p, "s1", now)
    db_conn.execute("UPDATE souls SET satiety = 85.0 WHERE soul_id = 's1'")
    db_conn.commit()
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert notes == []


def test_wallet_funding_notes_jev(db_conn):
    _insert_soul(db_conn, "s1", essence=0.0)
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    assert _think(p, "s1", now)["status"] == "dormant"
    assert notes == []
    db_conn.execute("UPDATE souls SET essence = 100.0 WHERE soul_id = 's1'")
    db_conn.commit()
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert ("s1", "wallet_delta") in notes


def test_wallet_small_delta_no_note(db_conn):
    _insert_soul(db_conn, "s1", essence=100.0)
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    _think(p, "s1", now)
    db_conn.execute("UPDATE souls SET essence = 105.0 WHERE soul_id = 's1'")
    db_conn.commit()
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert notes == []


def test_tamer_presence_change_notes_jev(db_conn, monkeypatch):
    _insert_soul(db_conn, "s1", custodian_id="t1")
    states = [{"presence": "active"}, {"presence": "idle"}]
    monkeypatch.setattr(presence, "get_presence", lambda tid, now=None: states.pop(0))
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    _think(p, "s1", now)
    assert notes == []
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert ("s1", "tamer_presence") in notes


def test_wallet_delta_fires_once(db_conn):
    _insert_soul(db_conn, "s1", essence=0.0)
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    _think(p, "s1", now)
    db_conn.execute("UPDATE souls SET essence = 100.0 WHERE soul_id = 's1'")
    db_conn.commit()
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert ("s1", "wallet_delta") in notes
    notes.clear()
    brain_busy.reset()
    _think(p, "s1", now + 2.0)
    assert ("s1", "wallet_delta") not in notes


def test_tamer_presence_shared_tamer_notes_each_soul(db_conn, monkeypatch):
    _insert_soul(db_conn, "s1", custodian_id="t1")
    _insert_soul(db_conn, "s2", custodian_id="t1")
    states = [
        {"presence": "active"},
        {"presence": "idle"},
        {"presence": "active"},
        {"presence": "idle"},
    ]
    monkeypatch.setattr(presence, "get_presence", lambda tid, now=None: states.pop(0))
    p = _think_pool()
    notes = []
    p.jev_note = lambda sid, kind: notes.append((sid, kind))
    now = time.time()
    _think(p, "s1", now)
    assert notes == []
    brain_busy.reset()
    _think(p, "s1", now + 1.0)
    assert ("s1", "tamer_presence") in notes
    notes.clear()
    brain_busy.reset()
    _think(p, "s2", now + 2.0)
    assert notes == []
    brain_busy.reset()
    _think(p, "s2", now + 3.0)
    assert ("s2", "tamer_presence") in notes
