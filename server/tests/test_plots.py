"""Plot grid, claim allocation & origin Commons (issue #19)."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from contextlib import contextmanager

import pytest

from .. import database
from .. import intents
from .. import persistence
from .. import plots
from ..world_tick import WorldTick

HUB_SECRET = "soulscape-secret-123"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fund():
    with database.get_db() as conn:
        return float(
            conn.execute(
                "SELECT value FROM globals WHERE key = 'essence_fund'"
            ).fetchone()["value"]
        )


def _essence(soul_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
        return float(row["essence"])


def _ledger_rows(intent_id):
    with database.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT entry_type, soul_id, amount FROM ledger "
                "WHERE intent_id = ? ORDER BY ledger_id",
                (intent_id,),
            )
        ]


def _escrow(intent_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, amount FROM escrows WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        return dict(row) if row else None


def _plot(plot_id):
    with database.get_db() as conn:
        return plots.get_plot(conn, plot_id)


def _counter():
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT value FROM globals WHERE key = 'plot_claim_seq'"
        ).fetchone()
        return int(float(row["value"]))


def _claim(soul_id, access_policy="open", idempotency_key=None):
    with database.get_db():
        record, _ = plots.enqueue_plot_intent(
            "sess-test",
            f"n-{soul_id}-{time.time_ns()}",
            None,
            soul_id,
            plots.KIND_PLOT_CLAIM,
            {"claimant_soul_id": soul_id, "access_policy": access_policy},
        )
    WorldTick().pump_intents()
    return intents.get_intent_by_nonce(record["session_id"], record["nonce"])


def test_grid_geometry():
    assert plots.grid_dims() == (16, 9)
    assert plots.origin_plot() == (8, 4)
    assert plots.commons_center() == (1020.0, 540.0)
    assert plots.plot_id_for(8, 4) == "8:4"
    assert plots.ring_of(8, 4) == 0
    assert plots.ring_of(7, 3) == 1
    assert plots.ring_of(0, 0) == 8
    assert plots.kind_of(8, 4) == "commons"
    assert plots.kind_of(5, 1) == "road"
    assert plots.kind_of(7, 3) == "claimable"
    assert plots.plot_at(900.0, 420.0) == (7, 3)
    assert plots.plot_at(1020.0, 540.0) == (8, 4)
    assert plots.is_road_ring(3) and plots.is_road_ring(6)
    assert not plots.is_road_ring(0) and not plots.is_road_ring(1)


def test_allocation_order_first_rings():
    ring1 = ["7:3", "7:4", "7:5", "8:3", "8:5", "9:3", "9:4", "9:5"]
    ring2 = [
        "6:2",
        "6:3",
        "6:4",
        "6:5",
        "6:6",
        "7:2",
        "7:6",
        "8:2",
        "8:6",
        "9:2",
        "9:6",
        "10:2",
        "10:3",
        "10:4",
        "10:5",
        "10:6",
    ]
    order = plots.allocation_order()
    assert order[:8] == ring1
    assert order[8:24] == ring2
    assert plots.plot_for_claim_seq(1) == "7:3"
    assert plots.plot_for_claim_seq(8) == "9:5"
    assert plots.plot_for_claim_seq(9) == "6:2"
    assert plots.plot_for_claim_seq(24) == "10:6"
    assert plots.plot_for_claim_seq(25) == "4:0"
    assert plots.plot_for_claim_seq(0) is None
    assert plots.plot_for_claim_seq(len(order) + 1) is None
    assert "8:4" not in order
    assert "5:1" not in order


def test_claim_fee_formula():
    assert plots.claim_fee(1) == 100.00
    assert plots.claim_fee(2) == 150.00
    assert plots.claim_fee(8) == 450.00


def test_seeded_grid_kinds(db_conn):
    commons = db_conn.execute(
        "SELECT plot_id FROM plots WHERE kind = 'commons'"
    ).fetchall()
    assert [r["plot_id"] for r in commons] == ["8:4"]
    roads = db_conn.execute(
        "SELECT DISTINCT ring FROM plots WHERE kind = 'road'"
    ).fetchall()
    assert sorted(r["ring"] for r in roads) == [3, 6]
    counts = db_conn.execute(
        "SELECT kind, COUNT(*) AS c FROM plots GROUP BY kind"
    ).fetchall()
    by_kind = {r["kind"]: r["c"] for r in counts}
    assert by_kind["commons"] == 1
    assert by_kind["road"] == 24 + 18
    assert by_kind["claimable"] == 144 - 1 - 42
    unowned = db_conn.execute(
        "SELECT COUNT(*) AS c FROM plots WHERE owner_id IS NOT NULL"
    ).fetchone()["c"]
    assert unowned == 0


def test_sequential_claims_get_distinct_expected_plots(client, register_soul):
    register_soul("c1", essence=500.0)
    register_soul("c2", essence=500.0)
    register_soul("c3", essence=500.0)
    expected = ["7:3", "7:4", "7:5"]
    for i, soul in enumerate(["c1", "c2", "c3"], start=1):
        res = client.post("/plots/claim", json={"soul_id": soul})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "success"
        assert body["plot_id"] == expected[i - 1]
        assert body["ring"] == 1
        assert body["fee"] == 100.00
        assert body["claim_seq"] == i
        plot = _plot(expected[i - 1])
        assert plot["owner_id"] == soul
        assert plot["claim_seq"] == i
        assert plot["access_policy"] == "open"
    assert _counter() == 3


def test_claim_fee_debit_atomic_success(client, register_soul):
    register_soul("rich", essence=500.0)
    before_fund = _fund()
    res = client.post("/plots/claim", json={"soul_id": "rich"})
    assert res.status_code == 200
    body = res.json()
    assert body["plot_id"] == "7:3"
    assert body["fee"] == 100.00
    assert _essence("rich") == 400.00
    assert _fund() == before_fund + 100.00
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT intent_id FROM intents WHERE soul_id = 'rich' "
            "AND kind = 'plot_claim'"
        ).fetchone()
        intent_id = row["intent_id"]
    rows = _ledger_rows(intent_id)
    assert [(r["entry_type"], r["soul_id"], r["amount"]) for r in rows] == [
        ("debit", "rich", 100.00),
        ("tax", None, 100.00),
    ]
    escrow = _escrow(intent_id)
    assert escrow["status"] == "applied"
    assert escrow["amount"] == 100.00


def test_claim_insufficient_funds_at_enqueue_no_intent(client, register_soul):
    register_soul("poor", essence=50.0)
    res = client.post("/plots/claim", json={"soul_id": "poor"})
    assert res.status_code == 400
    with database.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'plot_claim'"
        ).fetchone()["c"]
        assert count == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM escrows").fetchone()["c"] == 0
    assert _essence("poor") == 50.0
    assert _fund() == 0.0


def test_claim_fee_rise_at_adjudication_refunds(register_soul):
    for i in range(8):
        register_soul(f"whale{i}", essence=1000.0)
    register_soul("unlucky", essence=140.0)
    records = []
    for i in range(8):
        soul = f"whale{i}"
        record, _ = plots.enqueue_plot_intent(
            "sess-rise",
            f"rise-{i}",
            None,
            soul,
            plots.KIND_PLOT_CLAIM,
            {"claimant_soul_id": soul, "access_policy": "open"},
        )
        records.append(record)
    unlucky_rec, _ = plots.enqueue_plot_intent(
        "sess-rise",
        "rise-unlucky",
        None,
        "unlucky",
        plots.KIND_PLOT_CLAIM,
        {"claimant_soul_id": "unlucky", "access_policy": "open"},
    )
    WorldTick().pump_intents()
    got = intents.get_intent_by_nonce("sess-rise", "rise-unlucky")
    assert got["status"] == "rejected"
    assert got["result"]["reason"] == "insufficient_funds"
    assert _essence("unlucky") == 140.0
    escrow = _escrow(unlucky_rec["intent_id"])
    assert escrow["status"] == "released"
    assert _ledger_rows(unlucky_rec["intent_id"]) == []
    assert _plot("6:2")["owner_id"] is None
    for i, record in enumerate(records):
        got = intents.get_intent_by_nonce("sess-rise", f"rise-{i}")
        assert got["status"] == "adjudicated"
        assert got["result"]["claim_seq"] == i + 1


def test_claim_no_plots_left_refunds(register_soul):
    register_soul("late", essence=1000.0)
    record, _ = plots.enqueue_plot_intent(
        "sess-full",
        "full-1",
        None,
        "late",
        plots.KIND_PLOT_CLAIM,
        {"claimant_soul_id": "late", "access_policy": "open"},
    )
    with database.get_db() as conn:
        total = len(plots.allocation_order())
        conn.execute(
            "UPDATE globals SET value = ? WHERE key = 'plot_claim_seq'",
            (float(total),),
        )
        conn.commit()
    WorldTick().pump_intents()
    got = intents.get_intent_by_nonce("sess-full", "full-1")
    assert got["status"] == "rejected"
    assert got["result"]["reason"] == "no_plots_left"
    assert _essence("late") == 1000.0
    assert _escrow(record["intent_id"])["status"] == "released"
    assert _ledger_rows(record["intent_id"]) == []
    assert _fund() == 0.0


def test_claim_idempotency_key_single_charge(client, register_soul):
    register_soul("idem", essence=500.0)
    headers = {"Idempotency-Key": "claim-once-123"}
    first = client.post("/plots/claim", json={"soul_id": "idem"}, headers=headers)
    second = client.post("/plots/claim", json={"soul_id": "idem"}, headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["plot_id"] == second.json()["plot_id"] == "7:3"
    assert _essence("idem") == 400.00
    with database.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'plot_claim'"
        ).fetchone()["c"]
        assert count == 1


def _move_to(soul_id, custodian, x, y, nonce):
    intents.enqueue_intent(
        "sess-move", nonce, custodian, soul_id, "move_to", {"x": x, "y": y}
    )
    WorldTick().pump_intents()
    return intents.get_intent_by_nonce("sess-move", nonce)


def test_closed_plot_blocks_stranger_move_to(register_soul):
    register_soul("owner_a", essence=500.0)
    register_soul("stranger_b", essence=500.0)
    got = _claim("owner_a", access_policy="closed")
    assert got["status"] == "adjudicated"
    assert got["result"]["plot_id"] == "7:3"
    cx, cy = plots.plot_center(7, 3)
    stranger = _move_to("stranger_b", "owner_stranger_b", cx, cy, "m-blocked")
    assert stranger["status"] == "rejected"
    assert stranger["result"]["reason"] == "plot_closed"


def test_closed_plot_owner_and_operator_bypass(register_soul):
    register_soul("owner_a", essence=500.0)
    got = _claim("owner_a", access_policy="closed")
    assert got["status"] == "adjudicated"
    cx, cy = plots.plot_center(7, 3)
    owner = _move_to("owner_a", "owner_owner_a", cx, cy, "m-owner")
    assert owner["status"] == "adjudicated", owner["result"]
    op = _move_to("owner_a", None, cx, cy, "m-operator")
    assert op["status"] == "adjudicated", op["result"]


def test_move_onto_open_and_road_plots_allowed(register_soul):
    register_soul("roamer", essence=500.0)
    cx, cy = plots.plot_center(7, 3)
    got = _move_to("roamer", "owner_roamer", cx, cy, "m-open")
    assert got["status"] == "adjudicated", got["result"]
    rx, ry = plots.plot_center(5, 1)
    road = _move_to("roamer", "owner_roamer", rx, ry, "m-road")
    assert road["status"] == "adjudicated", road["result"]
    ccx, ccy = plots.commons_center()
    commons = _move_to("roamer", "owner_roamer", ccx, ccy, "m-commons")
    assert commons["status"] == "adjudicated", commons["result"]


def _neighbors(gx, gy):
    cols, rows = plots.grid_dims()
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = gx + dx, gy + dy
        if 0 <= nx < cols and 0 <= ny < rows:
            yield nx, ny


def test_road_ring_connectivity_around_closed_plot(register_soul):
    register_soul("owner_a", essence=500.0)
    register_soul("stranger_b", essence=500.0)
    got = _claim("owner_a", access_policy="closed")
    assert got["status"] == "adjudicated"
    closed = got["result"]["plot_id"]

    with database.get_db() as conn:
        ring3 = {
            r["plot_id"]
            for r in conn.execute(
                "SELECT plot_id FROM plots WHERE kind = 'road' AND ring = 3"
            )
        }
        assert len(ring3) == 24
        start = next(iter(ring3))
        seen = {start}
        queue = deque([start])
        while queue:
            pid = queue.popleft()
            gx, gy = (int(v) for v in pid.split(":"))
            for nx, ny in _neighbors(gx, gy):
                npid = plots.plot_id_for(nx, ny)
                if npid in ring3 and npid not in seen:
                    seen.add(npid)
                    queue.append(npid)
        assert seen == ring3

        road_all = {
            r["plot_id"]
            for r in conn.execute(
                "SELECT plot_id, grid_x, grid_y FROM plots WHERE kind = 'road'"
            )
        }
        for pid in road_all:
            gx, gy = (int(v) for v in pid.split(":"))
            cx, cy = plots.plot_center(gx, gy)
            assert plots.can_enter_plot(conn, "stranger_b", cx, cy)

        blocked = {closed}
        src, dst = plots.plot_id_for(6, 3), plots.plot_id_for(9, 3)
        seen = {src}
        prev: dict[str, str | None] = {src: None}
        queue = deque([src])
        while queue:
            pid = queue.popleft()
            if pid == dst:
                break
            gx, gy = (int(v) for v in pid.split(":"))
            for nx, ny in _neighbors(gx, gy):
                npid = plots.plot_id_for(nx, ny)
                if npid not in blocked and npid not in seen:
                    seen.add(npid)
                    prev[npid] = pid
                    queue.append(npid)
        assert dst in prev
        path = []
        node: str | None = dst
        while node is not None:
            path.append(node)
            node = prev[node]
        path.reverse()
        assert closed not in path
        for pid in path:
            gx, gy = (int(v) for v in pid.split(":"))
            cx, cy = plots.plot_center(gx, gy)
            assert plots.can_enter_plot(conn, "stranger_b", cx, cy)


def test_tick_stops_at_closed_plot_boundary(register_soul):
    register_soul("owner_a", essence=500.0)
    register_soul("walker", essence=500.0)
    got = _claim("owner_a", access_policy="closed")
    assert got["status"] == "adjudicated"
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET position = ?, velocity = ? WHERE soul_id = ?",
            ("[830.0, 420.0]", "[600.0, 0.0]", "walker"),
        )
        conn.commit()
    persistence.dirty.clear()
    WorldTick().step()
    unflushed = persistence.dirty_get("walker")
    assert unflushed is not None
    assert unflushed["position"] == [830.0, 420.0]
    assert unflushed["velocity"] == [0.0, 0.0]
    assert unflushed["move_target"] is None
    persistence.dirty.clear()


def test_newborn_spawns_in_commons(client, register_soul):
    register_soul("newbie", essence=100.0)
    res = client.get("/souls")
    assert res.status_code == 200
    souls = {s["soul_id"]: s for s in res.json()}
    assert souls["newbie"]["position"] == list(plots.commons_center())


def test_plot_claim_validators():
    ok, err = intents.validate_payload(
        "plot_claim",
        {"claimant_soul_id": "s1", "access_policy": "closed"},
    )
    assert err is None
    assert ok == {"claimant_soul_id": "s1", "access_policy": "closed"}
    ok, err = intents.validate_payload("plot_claim", {"claimant_soul_id": "s1"})
    assert err is None and ok["access_policy"] == "open"
    _, err = intents.validate_payload("plot_claim", {"claimant_soul_id": ""})
    assert err == "BAD_PAYLOAD"
    _, err = intents.validate_payload(
        "plot_claim", {"claimant_soul_id": "s1", "access_policy": "vip"}
    )
    assert err == "BAD_PAYLOAD"


def test_get_plots_lists_grid(client):
    res = client.get("/plots")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 144
    by_id = {p["plot_id"]: p for p in body}
    assert by_id["8:4"]["kind"] == "commons"
    assert by_id["5:1"]["kind"] == "road"
    assert by_id["7:3"]["kind"] == "claimable"
    assert by_id["7:3"]["access_policy"] == "open"
    assert by_id["7:3"]["owner_id"] is None


def test_set_access_policy_operator_and_errors(client, register_soul):
    register_soul("owner_a", essence=500.0)
    got = _claim("owner_a")
    assert got["status"] == "adjudicated"
    res = client.post("/plots/7:3/access", json={"access_policy": "closed"})
    assert res.status_code == 200
    assert res.json()["access_policy"] == "closed"
    assert _plot("7:3")["access_policy"] == "closed"
    res = client.post("/plots/7:3/access", json={"access_policy": "open"})
    assert res.status_code == 200
    bad = client.post("/plots/7:3/access", json={"access_policy": "vip"})
    assert bad.status_code == 400
    commons = client.post("/plots/8:4/access", json={"access_policy": "closed"})
    assert commons.status_code == 400
    missing = client.post("/plots/99:99/access", json={"access_policy": "closed"})
    assert missing.status_code == 404


@contextmanager
def _swapped_db(path: str):
    old = database.DB_PATH
    database.DB_PATH = path
    try:
        yield
    finally:
        database.DB_PATH = old


def _seed_crash_db(path: str) -> None:
    with _swapped_db(path):
        database.init_db()
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO souls (soul_id, owner_id, custodian_id, essence, "
                "position) VALUES ('crash_buyer', 'o_b', 'o_b', 500.0, "
                "'[0, 0]')"
            )
            conn.commit()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_health(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise AssertionError(f"server on {port} never became healthy")


def _start_server(db_path: str, port: int, authoritative: bool, log_path: str):
    env = dict(os.environ)
    env.update(
        {
            "SOULSCAPE_DB_PATH": db_path,
            "HUB_SECRET_KEY": HUB_SECRET,
            "HOST": "127.0.0.1",
            "PORT": str(port),
        }
    )
    if authoritative:
        env["HUB_AUTHORITATIVE"] = "1"
    else:
        env.pop("HUB_AUTHORITATIVE", None)
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def _start_sim(db_path: str, sim_port: int, log_path: str):
    """Start a real sim subprocess (issue #37): the world owner.

    After the API "crashes" with a pending intent, the sim boots on the
    same DB, runs boot recovery (re-pumps pending intents), and its tick
    loop adjudicates -- exactly the production recovery path.
    """
    env = dict(os.environ)
    env.update(
        {
            "SOULSCAPE_DB_PATH": db_path,
            "SIM_PORT": str(sim_port),
            "HUB_SECRET_KEY": HUB_SECRET,
        }
    )
    env.pop("SOULSCAPE_SIM_MODE", None)
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.sim_process"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def _wait_sim(sim_port: int, timeout: float = 60.0) -> None:
    from ..sim_ipc import SimClient

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client = SimClient(port=sim_port)
            try:
                resp = client.call({"type": "ping"}, timeout=5.0)
            finally:
                client.close()
            if resp.get("ok"):
                return
        except Exception:
            time.sleep(0.25)
    raise AssertionError(f"sim on {sim_port} never became reachable")


def _intent_status(intent_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        return row["status"], json.loads(row["result"]) if row["result"] else None


def _wait_intent_settled(db_path: str, intent_id: str, timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _swapped_db(db_path):
            status, result = _intent_status(intent_id)
        if status != "pending":
            return status, result
        time.sleep(0.25)
    raise AssertionError("intent never settled")


@pytest.mark.anyio
async def test_crash_between_ack_and_settlement_settles_once(tmp_path):
    pytest.importorskip("websockets")
    import websockets

    db_path = str(tmp_path / "crash_plot.db")
    _seed_crash_db(db_path)
    port = _free_port()

    proc1, log1 = _start_server(db_path, port, False, str(tmp_path / "s1.log"))
    try:
        await asyncio.to_thread(_wait_health, port)
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws/op1",
            additional_headers={"X-Hub-Secret": HUB_SECRET},
        ) as ws:
            connected = json.loads(await ws.recv())
            assert connected["type"] == "connected"
            assert json.loads(await ws.recv())["type"] == "snapshot"
            frame = {
                "v": 1,
                "type": "intent",
                "kind": "plot_claim",
                "nonce": "nonce-crash-plot",
                "session_id": connected["session_id"],
                "soul_id": "crash_buyer",
                "claimant_soul_id": "crash_buyer",
                "access_policy": "open",
            }
            frame["signature"] = intents.sign_intent(frame, connected["hmac_key"])
            await ws.send(json.dumps(frame))
            ack = json.loads(await ws.recv())
            assert ack["type"] == "intent_ack"
            assert ack["status"] == "accepted"
            intent_id = ack["intent_id"]
        with _swapped_db(db_path):
            status, _ = _intent_status(intent_id)
            assert status == "pending"
            assert _essence("crash_buyer") == 400.0
    finally:
        proc1.kill()
        proc1.wait(timeout=15)
        log1.close()

    sim_port = _free_port()
    proc2, log2 = _start_sim(db_path, sim_port, str(tmp_path / "sim.log"))
    try:
        await asyncio.to_thread(_wait_sim, sim_port)
        status, result = await asyncio.to_thread(
            _wait_intent_settled, db_path, intent_id
        )
        assert status == "adjudicated", result
        assert result["plot_id"] == "7:3"
        assert result["fee"] == 100.00
        with _swapped_db(db_path):
            assert _essence("crash_buyer") == 400.0
            assert _fund() == 100.0
            rows = _ledger_rows(intent_id)
            assert [r["entry_type"] for r in rows] == ["debit", "tax"]
            assert [r["amount"] for r in rows] == [100.0, 100.0]
            assert _escrow(intent_id)["status"] == "applied"
            plot = _plot("7:3")
            assert plot["owner_id"] == "crash_buyer"
            assert plot["claim_seq"] == 1
            assert _counter() == 1
    finally:
        proc2.kill()
        proc2.wait(timeout=15)
        log2.close()
