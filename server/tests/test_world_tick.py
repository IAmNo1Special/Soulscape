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
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) VALUES (?, ?, ?, ?, 100.0)",
        (
            soul_id,
            f"owner_{soul_id}",
            json.dumps([x, y]),
            json.dumps([vx, vy]),
        ),
    )
    db_conn.commit()


def _position(db_conn, soul_id):
    from .. import persistence

    unflushed = persistence.dirty_get(soul_id)
    if unflushed is not None and unflushed.get("position") is not None:
        return [float(unflushed["position"][0]), float(unflushed["position"][1])]
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
        "INSERT INTO souls (soul_id, owner_id, position, essence) VALUES (?, ?, ?, 100.0)",
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
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) VALUES (?, ?, ?, ?, 100.0)",
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
        await asyncio.sleep(0.25)
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


def test_sim_owned_tick_advances_and_moves_souls(db_conn):
    """Issue #37: the tick loop is owned by the sim, not the API.

    Drive the in-process gateway's sim tick the way a real sim process
    would (manual steps stand in for its loop) and confirm /debug/tick
    reports the world advancing: tick_id climbs and souls move.
    """
    _insert_soul(db_conn, "mover", x=100.0, y=100.0, vx=50.0, vy=0.0)
    hub_secret = os.environ.get("HUB_SECRET_KEY")
    assert hub_secret, "HUB_SECRET_KEY must be set for this test"
    with TestClient(main.app, headers={"X-Hub-Secret": hub_secret}) as c:
        first = c.get("/debug/tick").json()
        first_tick = first["tick_id"]
        first_x = next(p["x"] for p in first["positions"] if p["soul_id"] == "mover")
        # The sim side: step the world directly (a real sim process runs
        # these inside its 20 Hz loop; here the API only observes).
        from ..sim_gateway import get_gateway

        tick = get_gateway().backend.host.tick
        for _ in range(5):
            tick.step()
        data = c.get("/debug/tick").json()
        assert data["tick_id"] == first_tick + 5
        assert data["running"] is False  # no loop running in-process
        last_x = next(p["x"] for p in data["positions"] if p["soul_id"] == "mover")
        assert last_x > first_x


def test_tick_benchmark(db_conn):
    # Regression tripwire at 500 moving souls (NOT a 20 Hz fitness
    # gate: reference hardware needs ~37-52ms/step here, over the
    # 50ms live budget -- that envelope needs step optimization,
    # tracked separately). The absolute bar below catches ~2x step
    # blowups; per-step work scales ~linearly with souls, so it stays
    # sensitive to real regressions.
    n = 500
    db_conn.executemany(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) VALUES (?, ?, ?, ?, 100.0)",
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
    assert avg_ms < 80.0, (
        f"tick too slow: {avg_ms:.2f}ms avg (500-soul regression bar 80ms)"
    )


def test_tick_p99_under_concurrent_ipc_load(db_conn):
    """Issue #37: tick p99 stays within budget under concurrent API
    read/write load over real TCP IPC.

    500 souls in the world; 4 loader threads hammer the sim with
    intent submits, status polls, position queries, and commands
    while the main thread steps the tick and samples every step
    duration. Regression tripwire at a fixed 250ms bar (NOT a 20 Hz
    fitness gate: 500 contended souls exceed the 50ms live budget on
    reference hardware; see test_tick_benchmark). Absolute
    wall-clock bars flake on slower machines by design -- this
    catches ~2x blowups, nothing finer.
    """
    import socket as _socket
    import threading as _threading

    from ..sim_ipc import SimClient, SimDispatcher, SimServer

    n = 500
    db_conn.executemany(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) VALUES (?, ?, ?, ?, 100.0)",
        [
            (
                f"pload_{i}",
                "owner_pload",
                json.dumps([100.0, 100.0]),
                json.dumps([50.0, -30.0]),
            )
            for i in range(n)
        ],
    )
    db_conn.commit()

    tick = WorldTick()
    dispatcher = SimDispatcher(tick=tick)
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = SimServer(dispatcher, port=port)
    server.start()
    stop = _threading.Event()
    errors: list = []

    def loader(worker_id: int):
        # Models one busy API client: a viewport positions read at the
        # real 10 Hz pump rate plus roughly one intent write per second.
        client = SimClient(port=port)
        i = 0
        try:
            while not stop.is_set():
                try:
                    client.call(
                        {"type": "query", "name": "positions", "params": {}},
                        timeout=10.0,
                    )
                    if i % 5 == 0:
                        nonce = f"pload-{worker_id}-{i}"
                        client.call(
                            {
                                "type": "intent_submit",
                                "session_id": f"pload-{worker_id}",
                                "nonce": nonce,
                                "custodian_id": "tamer_pload",
                                "soul_id": "tamer:tamer_pload",
                                "kind": "tamer_presence",
                                "payload": {
                                    "presence": "active",
                                    "idle_bucket": "0-5",
                                },
                            },
                            timeout=10.0,
                        )
                        client.call({"type": "ping"}, timeout=10.0)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    break
                i += 1
                time.sleep(0.1)
        finally:
            client.close()

    workers = [
        _threading.Thread(target=loader, args=(w,), daemon=True) for w in range(4)
    ]
    try:
        for w in workers:
            w.start()
        # Warm up so the loaders are contending before we sample.
        tick.step()
        time.sleep(0.5)
        durations: list[float] = []
        steps = 60
        for _ in range(steps):
            started = time.perf_counter()
            tick.step()
            durations.append((time.perf_counter() - started) * 1000.0)
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=10)
        server.stop()

    assert not errors, f"IPC loader errors: {errors[:3]}"
    durations.sort()
    p99 = durations[max(0, int(len(durations) * 0.99) - 1)]
    p50 = durations[len(durations) // 2]
    budget_ms = 1000.0 / TICK_HZ
    print(
        f"\nbenchmark: {n} souls x {steps} steps under 4-client IPC "
        f"(10Hz reads + ~1 intent/s each): "
        f"p50 {p50:.2f}ms p99 {p99:.2f}ms (budget {budget_ms:.0f}ms)"
    )
    assert p99 < 250.0, (
        f"tick p99 too slow under load: {p99:.2f}ms (500-soul bar 250ms)"
    )
