import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from .. import database
from .. import main
from ..world_tick import TICK_DT, TICK_HZ, WorldTick, hub_authoritative_enabled


def _insert_soul(db_conn, soul_id, x=100.0, y=200.0, vx=0.0, vy=0.0):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity) VALUES (?, ?, ?, ?)",
        (
            soul_id,
            f"owner_{soul_id}",
            json.dumps([x, y]),
            json.dumps([vx, vy]),
        ),
    )
    db_conn.commit()


def _position(db_conn, soul_id):
    row = db_conn.execute(
        "SELECT position FROM souls WHERE soul_id = ?", (soul_id,)
    ).fetchone()
    return json.loads(row["position"])


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("HUB_AUTHORITATIVE", raising=False)
    assert hub_authoritative_enabled() is False


def test_flag_on_via_env(monkeypatch):
    monkeypatch.setenv("HUB_AUTHORITATIVE", "1")
    assert hub_authoritative_enabled() is True


def test_step_integrates_velocity(db_conn):
    _insert_soul(db_conn, "s1", x=100.0, y=200.0, vx=10.0, vy=-5.0)
    tick = WorldTick()
    assert tick.step() == 1
    assert tick.tick_id == 1
    assert tick.souls_moved_last_tick == 1
    x, y = _position(db_conn, "s1")
    assert x == pytest.approx(100.0 + 10.0 * TICK_DT)
    assert y == pytest.approx(200.0 - 5.0 * TICK_DT)


def test_step_skips_stationary_souls(db_conn):
    _insert_soul(db_conn, "s1", vx=0.0, vy=0.0)
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position) VALUES (?, ?, ?)",
        ("s2", "owner_s2", json.dumps([50.0, 50.0])),
    )
    db_conn.commit()
    tick = WorldTick()
    assert tick.step() == 0
    assert tick.tick_id == 1
    assert tick.souls_moved_last_tick == 0
    assert _position(db_conn, "s1") == [100.0, 200.0]
    assert _position(db_conn, "s2") == [50.0, 50.0]


def test_step_clamps_to_screen_bounds(db_conn):
    width, _ = database.SCREEN_BOUNDS
    _insert_soul(db_conn, "s1", x=width - 15.0, y=500.0, vx=100.0, vy=0.0)
    WorldTick().step()
    x, y = _position(db_conn, "s1")
    assert x == pytest.approx(width - 10.0)
    assert y == pytest.approx(500.0)


def test_step_ignores_malformed_rows(db_conn):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity) VALUES (?, ?, ?, ?)",
        ("bad", "owner_bad", "not-json", "[1, 2, 3]"),
    )
    db_conn.commit()
    tick = WorldTick()
    assert tick.step() == 0
    assert tick.tick_id == 1


def test_migration_adds_velocity_column(tmp_path):
    import sqlite3

    path = str(tmp_path / "mig.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE souls (soul_id TEXT PRIMARY KEY, owner_id TEXT, "
        "position TEXT, secret_prefix TEXT)"
    )
    conn.commit()
    conn.close()
    old = database.DB_PATH
    database.DB_PATH = path
    try:
        database.init_db()
        conn = sqlite3.connect(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(souls)")}
        conn.close()
    finally:
        database.DB_PATH = old
    assert "velocity" in cols


def test_post_souls_accepts_velocity(client, db_conn):
    payload = {
        "owner_id": "o1",
        "souls": [{"soul_id": "v1", "name": "V", "velocity": [30.0, -20.0]}],
    }
    res = client.post("/souls", json=payload)
    assert res.status_code == 200
    row = db_conn.execute("SELECT velocity FROM souls WHERE soul_id = 'v1'").fetchone()
    assert json.loads(row["velocity"]) == [30.0, -20.0]


def test_post_souls_clamps_velocity(client, db_conn):
    payload = {
        "owner_id": "o1",
        "souls": [{"soul_id": "v2", "name": "V", "velocity": [9999.0, -9999.0]}],
    }
    res = client.post("/souls", json=payload)
    assert res.status_code == 200
    row = db_conn.execute("SELECT velocity FROM souls WHERE soul_id = 'v2'").fetchone()
    assert json.loads(row["velocity"]) == [500.0, -500.0]


def test_get_souls_returns_velocity(client):
    payload = {
        "owner_id": "o1",
        "souls": [{"soul_id": "v3", "name": "V", "velocity": [12.5, 7.5]}],
    }
    assert client.post("/souls", json=payload).status_code == 200
    res = client.get("/souls", params={"owner_id": "o1"})
    assert res.status_code == 200
    souls = {s["soul_id"]: s for s in res.json()}
    assert souls["v3"]["velocity"] == [12.5, 7.5]


def test_debug_endpoint_flag_off_reports_disabled(client):
    res = client.get("/debug/tick")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is False
    assert data["tick_id"] == 0
    assert data["positions"] == []


def test_debug_endpoint_requires_operator(client, register_soul):
    register_soul("op1", name="Op Soul", secret="s" * 32)
    user_client = TestClient(main.app, headers={"X-Hub-Secret": "s" * 32})
    res = user_client.get("/debug/tick")
    assert res.status_code == 403


def test_start_stop_loop(db_conn):
    async def go():
        tick = WorldTick(tick_dt=0.01)
        await tick.start()
        assert tick.running is True
        assert tick.enabled is True
        await asyncio.sleep(0.07)
        await tick.stop()
        return tick

    tick = asyncio.run(go())
    assert tick.tick_id >= 4
    assert tick.running is False
    assert tick.enabled is False


def test_flag_off_lifespan_starts_no_tick(monkeypatch):
    monkeypatch.delenv("HUB_AUTHORITATIVE", raising=False)
    secret = os.getenv("HUB_SECRET_KEY", "soulscape-secret-123")
    with TestClient(main.app, headers={"X-Hub-Secret": secret}) as c:
        data = c.get("/debug/tick").json()
        assert data["enabled"] is False
        assert data["running"] is False
        time.sleep(0.3)
        assert c.get("/debug/tick").json()["tick_id"] == 0


def test_flag_on_lifespan_advances_ticks(monkeypatch, db_conn):
    monkeypatch.setenv("HUB_AUTHORITATIVE", "1")
    _insert_soul(db_conn, "mover", x=100.0, y=100.0, vx=50.0, vy=0.0)
    secret = os.getenv("HUB_SECRET_KEY", "soulscape-secret-123")
    with TestClient(main.app, headers={"X-Hub-Secret": secret}) as c:
        data = c.get("/debug/tick").json()
        assert data["enabled"] is True
        assert data["running"] is True
        assert data["tick_hz"] == TICK_HZ
        first_tick = data["tick_id"]
        first_x = next(p["x"] for p in data["positions"] if p["soul_id"] == "mover")
        deadline = time.time() + 5.0
        advanced = False
        while time.time() < deadline:
            time.sleep(0.2)
            data = c.get("/debug/tick").json()
            if data["tick_id"] > first_tick:
                advanced = True
                break
        assert advanced, "tick_id did not advance with flag on"
        last_x = next(p["x"] for p in data["positions"] if p["soul_id"] == "mover")
        assert last_x > first_x
        assert data["last_tick_duration_ms"] >= 0.0


def test_tick_benchmark(db_conn):
    n = 500
    db_conn.executemany(
        "INSERT INTO souls (soul_id, owner_id, position, velocity) VALUES (?, ?, ?, ?)",
        [
            (
                f"bench_{i}",
                "owner_bench",
                json.dumps([100.0, 100.0]),
                json.dumps([50.0, -30.0]),
            )
            for i in range(n)
        ],
    )
    db_conn.commit()
    tick = WorldTick()
    tick.step()
    steps = 20
    start = time.perf_counter()
    for _ in range(steps):
        tick.step()
    avg_ms = (time.perf_counter() - start) * 1000.0 / steps
    budget_ms = 1000.0 / TICK_HZ
    print(
        f"\nbenchmark: {n} souls x {steps} steps: "
        f"avg {avg_ms:.2f}ms per tick (budget {budget_ms:.0f}ms)"
    )
    assert avg_ms < budget_ms * 0.1, (
        f"tick too slow: {avg_ms:.2f}ms avg vs {budget_ms:.0f}ms budget"
    )
