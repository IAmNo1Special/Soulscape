"""Marketplace through intent adjudication + ledger (issue #17)."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager

import pytest
import websockets
from fastapi.testclient import TestClient

from .. import database
from .. import intents
from .. import market
from .. import persistence
from ..world_tick import WorldTick

HUB_SECRET = os.getenv("HUB_SECRET_KEY", "soulscape-secret-123")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


def _essence(soul_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
        return float(row["essence"])


def _fund():
    with database.get_db() as conn:
        return float(
            conn.execute(
                "SELECT value FROM globals WHERE key = 'essence_fund'"
            ).fetchone()["value"]
        )


def _escrow_statuses():
    with database.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT intent_id, status, amount FROM escrows")
        ]


def _intent_status(intent_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return row["status"], json.loads(row["result"]) if row["result"] else None


def _journal_types():
    with database.get_db() as conn:
        return [
            r["type"] for r in conn.execute("SELECT type FROM journal ORDER BY seq")
        ]


def _list_via_rest(client, seller_id, price, item=None):
    res = client.post(
        "/marketplace/list",
        json={
            "seller_id": seller_id,
            "seller_name": "Seller",
            "item": item or {"name": "Orb"},
            "price": price,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"
    return res.json()["listing_id"]


def test_rest_list_buy_settles_synchronously(client, register_soul):
    register_soul("mk_seller", essence=100.0)
    register_soul("mk_buyer", essence=500.0)
    listing_id = _list_via_rest(client, "mk_seller", 50.0)

    res = client.post(f"/marketplace/buy/{listing_id}", json={"buyer_id": "mk_buyer"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"
    assert data["tax_collected"] == 1.0
    assert data["seller_credited"] == 49.0
    assert data["seller_id"] == "mk_seller"

    assert _essence("mk_buyer") == 450.0
    assert _essence("mk_seller") == 149.0
    assert _fund() == 1.0

    with database.get_db() as conn:
        intent_id = conn.execute(
            "SELECT intent_id FROM intents WHERE kind = 'market_buy'"
        ).fetchone()["intent_id"]
    rows = _ledger_rows(intent_id)
    assert [(r["entry_type"], r["soul_id"], r["amount"]) for r in rows] == [
        ("debit", "mk_buyer", 50.0),
        ("credit", "mk_seller", 49.0),
        ("tax", None, 1.0),
    ]
    assert _escrow_statuses() == [
        {"intent_id": intent_id, "status": "applied", "amount": 50.0}
    ]
    assert "intent_adjudicated" in _journal_types()

    res = client.get("/marketplace")
    assert res.json()["listings"] == []


def test_rest_buy_idempotency_key(client, register_soul):
    register_soul("id_seller", essence=100.0)
    register_soul("id_buyer", essence=500.0)
    listing_id = _list_via_rest(client, "id_seller", 40.0)

    headers = {"Idempotency-Key": "buy-once-123"}
    first = client.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_id": "id_buyer"},
        headers=headers,
    )
    second = client.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_id": "id_buyer"},
        headers=headers,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "success"
    assert second.json()["status"] == "success"

    assert _essence("id_buyer") == 460.0
    assert _essence("id_seller") == 139.2
    assert _fund() == 0.8
    with database.get_db() as conn:
        n_intents = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'market_buy'"
        ).fetchone()["c"]
        n_debits = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE entry_type = 'debit'"
        ).fetchone()["c"]
    assert n_intents == 1
    assert n_debits == 1


def test_rest_buy_insufficient_funds_no_intent(client, register_soul):
    register_soul("poor_seller", essence=100.0)
    register_soul("poor_buyer", essence=10.0)
    listing_id = _list_via_rest(client, "poor_seller", 50.0)

    res = client.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_id": "poor_buyer"}
    )
    assert res.status_code == 400
    assert _essence("poor_buyer") == 10.0
    with database.get_db() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM intents").fetchone()["c"] == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM escrows").fetchone()["c"] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM marketplace WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()["c"]
            == 1
        )


def test_concurrent_buys_settle_exactly_once(client, register_soul):
    register_soul("cb_seller", essence=100.0)
    buyers = [f"cb_buyer{i}" for i in range(10)]
    for buyer in buyers:
        register_soul(buyer, essence=500.0)
    listing_id = _list_via_rest(client, "cb_seller", 100.0)

    outcomes = {}
    lock = threading.Lock()

    def attempt(buyer):
        try:
            res = client.post(
                f"/marketplace/buy/{listing_id}", json={"buyer_id": buyer}
            )
            code = res.status_code
        except Exception as exc:
            code = f"error: {exc}"
        with lock:
            outcomes[buyer] = code

    threads = [threading.Thread(target=attempt, args=(b,)) for b in buyers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(outcomes.values())
    assert codes == [200] + [404] * 9, outcomes

    winner = next(b for b, c in outcomes.items() if c == 200)
    assert _essence(winner) == 400.0
    for buyer in buyers:
        if buyer != winner:
            assert _essence(buyer) == 500.0, buyer
    assert _essence("cb_seller") == 198.0
    assert _fund() == 2.0

    with database.get_db() as conn:
        n_buys = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'market_buy' "
            "AND status = 'adjudicated'"
        ).fetchone()["c"]
        n_rejected = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'market_buy' "
            "AND status = 'rejected'"
        ).fetchone()["c"]
        n_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'market_buy' "
            "AND status = 'pending'"
        ).fetchone()["c"]
        n_debits = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE entry_type = 'debit'"
        ).fetchone()["c"]
        n_held = conn.execute(
            "SELECT COUNT(*) AS c FROM escrows WHERE status = 'held'"
        ).fetchone()["c"]
        n_released = conn.execute(
            "SELECT COUNT(*) AS c FROM escrows WHERE status = 'released'"
        ).fetchone()["c"]
        n_applied = conn.execute(
            "SELECT COUNT(*) AS c FROM escrows WHERE status = 'applied'"
        ).fetchone()["c"]
    assert n_buys == 1
    assert n_pending == 0
    assert n_debits == 1
    assert n_held == 0
    # Every buy intent that got past ingress settled; every loser that
    # held an escrow got it released. (Buys arriving after the listing
    # was claimed are refused at ingress with 404 and never hold funds.)
    assert n_applied == 1
    assert n_released == n_rejected
    res = client.get("/marketplace")
    assert res.json()["listings"] == []


def test_conservation_across_prices(client, register_soul):
    register_soul("cv_seller", essence=1000.0)
    register_soul("cv_buyer", essence=100000.0)
    prices = [10.0, 33.33, 99.99, 7.5, 250.0, 0.99]
    for price in prices:
        listing_id = _list_via_rest(client, "cv_seller", price)
        res = client.post(
            f"/marketplace/buy/{listing_id}", json={"buyer_id": "cv_buyer"}
        )
        assert res.status_code == 200, res.text

    with database.get_db() as conn:
        sums = conn.execute(
            "SELECT entry_type, ROUND(SUM(amount), 2) AS total FROM ledger "
            "GROUP BY entry_type"
        ).fetchall()
    totals = {r["entry_type"]: r["total"] for r in sums}
    assert round(totals["debit"], 2) == round(
        totals["credit"] + totals["tax"], 2
    )
    assert totals["debit"] == round(sum(prices), 2)
    assert _fund() == pytest.approx(totals["tax"], abs=1e-9)

    with database.get_db() as conn:
        report = market.verify_balances(conn)
    assert report["ok"] is True, report
    assert report["soul_drifts"] == []


def test_verify_balances_detects_and_repairs(client, register_soul):
    register_soul("vb_seller", essence=100.0)
    register_soul("vb_buyer", essence=500.0)
    listing_id = _list_via_rest(client, "vb_seller", 50.0)
    res = client.post(f"/marketplace/buy/{listing_id}", json={"buyer_id": "vb_buyer"})
    assert res.status_code == 200

    with database.get_db() as conn:
        report = market.verify_balances(conn)
        assert report["ok"] is True

        conn.execute(
            "UPDATE souls SET essence = essence + 25.0 WHERE soul_id = 'vb_buyer'"
        )
        conn.execute("UPDATE globals SET value = value + 3.0 WHERE key = 'essence_fund'")
        conn.commit()

        report = market.verify_balances(conn)
        assert report["ok"] is False
        assert len(report["soul_drifts"]) == 1
        assert report["soul_drifts"][0]["soul_id"] == "vb_buyer"
        assert report["soul_drifts"][0]["drift"] == pytest.approx(25.0)
        assert report["fund"]["drift"] == pytest.approx(3.0)

        report = market.verify_balances(conn, repair=True)
        assert report["repaired"] is True
        assert report["ok"] is True

    assert _essence("vb_buyer") == 450.0
    assert _fund() == 1.0
    with database.get_db() as conn:
        assert market.verify_balances(conn)["ok"] is True


def test_buy_rejected_when_listing_gone_releases_escrow(client, register_soul):
    register_soul("rg_seller", essence=100.0)
    register_soul("rg_buyer", essence=500.0)
    listing_id = _list_via_rest(client, "rg_seller", 60.0)

    record, created = market.enqueue_market_intent(
        "sess_rg",
        "n_rg",
        "owner_rg_buyer",
        "rg_buyer",
        market.KIND_MARKET_BUY,
        {"listing_id": listing_id, "buyer_soul_id": "rg_buyer"},
    )
    assert created is True
    assert _essence("rg_buyer") == 440.0

    with database.get_db() as conn:
        conn.execute("DELETE FROM marketplace WHERE listing_id = ?", (listing_id,))
        conn.commit()

    WorldTick().pump_intents()
    status, result = _intent_status(record["intent_id"])
    assert status == "rejected"
    assert result["reason"] == "listing_gone"

    assert _essence("rg_buyer") == 500.0
    assert _escrow_statuses()[0]["status"] == "released"
    assert _ledger_rows(record["intent_id"]) == []
    assert "intent_rejected" in _journal_types()


def test_cancel_by_seller_via_rest(client, register_soul):
    register_soul("cx_seller", essence=100.0)
    listing_id = _list_via_rest(client, "cx_seller", 25.0)

    res = client.post(f"/marketplace/cancel/{listing_id}")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"

    res = client.get("/marketplace")
    assert res.json()["listings"] == []
    with database.get_db() as conn:
        memos = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE entry_type = 'memo'"
        ).fetchone()["c"]
    assert memos == 2


def test_cancel_missing_listing_404(client, register_soul):
    register_soul("cx2_seller", essence=100.0)
    res = client.post("/marketplace/cancel/nope")
    assert res.status_code == 404


def test_cancel_custody_denied_for_other_tamer(client, register_soul):
    register_soul("cz_seller", essence=100.0)
    listing_id = _list_via_rest(client, "cz_seller", 25.0)

    reg = client.post(
        "/tamers/register", json={"username": "mallory", "password": "password123"}
    )
    assert reg.status_code == 201
    login = client.post(
        "/tamers/login", json={"username": "mallory", "password": "password123"}
    )
    token = login.json()["token"]
    tamer_client = TestClient(
        client.app, headers={"X-Hub-Secret": token}
    )
    res = tamer_client.post(f"/marketplace/cancel/{listing_id}")
    assert res.status_code == 403, res.text
    res = client.get("/marketplace")
    assert len(res.json()["listings"]) == 1


def test_market_intent_validators():
    payload, error = intents.validate_payload(
        "market_list",
        {"item": {"n": 1}, "price": 5, "seller_soul_id": "s",
         "listing_id": "l1", "seller_name": "S"},
    )
    assert error is None and payload["price"] == 5.0
    for bad in (
        {"item": {"n": 1}, "price": 0, "seller_soul_id": "s"},
        {"item": {"n": 1}, "price": -3, "seller_soul_id": "s"},
        {"item": "not-a-dict", "price": 5, "seller_soul_id": "s"},
        {"item": {"n": 1}, "price": 5, "seller_soul_id": ""},
        {"item": {"n": 1}, "price": float("nan"), "seller_soul_id": "s"},
    ):
        assert intents.validate_payload("market_list", bad) == (None, "BAD_PAYLOAD")
    assert intents.validate_payload(
        "market_buy", {"listing_id": "l", "buyer_soul_id": "b"}
    )[1] is None
    assert intents.validate_payload("market_buy", {"listing_id": "l"}) == (
        None, "BAD_PAYLOAD",
    )
    assert intents.validate_payload("market_cancel", {"listing_id": "l"})[1] is None
    assert intents.validate_payload("market_cancel", {}) == (None, "BAD_PAYLOAD")


def test_reconcile_escrows_settles_against_intent_states(db_conn):
    now = 1700000000.0
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, essence) VALUES "
        "('e1', 'o1', 100.0), ('e2', 'o2', 100.0), "
        "('e3', 'o3', 100.0), ('e4', 'o4', 100.0)"
    )
    db_conn.executemany(
        "INSERT INTO intents (intent_id, session_id, nonce, soul_id, kind, "
        "payload, status, created_at) VALUES (?, ?, ?, ?, 'market_buy', "
        "'{}', ?, ?)",
        [
            ("ii_applied", "s", "n1", "e1", "adjudicated", now),
            ("ii_rejected", "s", "n2", "e2", "rejected", now),
            ("ii_pending", "s", "n3", "e3", "pending", now),
        ],
    )
    db_conn.executemany(
        "INSERT INTO escrows (escrow_id, intent_id, soul_id, amount, status, "
        "created_at) VALUES (?, ?, ?, 10.0, 'held', ?)",
        [
            ("esc1", "ii_applied", "e1", now),
            ("esc2", "ii_rejected", "e2", now),
            ("esc3", "ii_pending", "e3", now),
            ("esc4", "ii_missing", "e4", now),
        ],
    )
    db_conn.commit()

    settled = persistence.reconcile_escrows(db_conn)
    assert settled == 3

    statuses = {
        r["escrow_id"]: r["status"]
        for r in db_conn.execute("SELECT escrow_id, status FROM escrows")
    }
    assert statuses == {
        "esc1": "applied",
        "esc2": "released",
        "esc3": "held",
        "esc4": "released",
    }
    assert _essence("e2") == 110.0
    assert _essence("e4") == 110.0
    assert _essence("e1") == 100.0
    assert _essence("e3") == 100.0


def test_journal_replay_tolerates_market_events(db_conn):
    state = {"s1": {"position": [1.0, 2.0], "velocity": [0.0, 0.0],
                    "move_target": None}}
    persistence.append_event(
        db_conn, 7, persistence.EVENT_INTENT_ADJUDICATED,
        {"intent_id": "ix", "kind": "market_buy", "soul_id": "s1",
         "result": {"price": 10.0}},
    )
    db_conn.commit()
    events = persistence.journal_tail(db_conn, 0)
    persistence.replay_tail(state, events, 7, 0.0, 0.2, (1920, 1080), exact=True)
    assert state["s1"]["position"] == [1.0, 2.0]

@contextmanager
def _open_ws(client: TestClient, path: str = "/ws/op1"):
    with client.websocket_connect(path) as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        assert connected["session_id"]
        assert connected["hmac_key"]
        assert ws.receive_json()["type"] == "snapshot"
        yield ws, connected


def _intent_frame(key, session_id, nonce, kind, soul_id, **fields):
    frame = {
        "v": 1,
        "type": "intent",
        "kind": kind,
        "nonce": nonce,
        "session_id": session_id,
        "soul_id": soul_id,
        **fields,
    }
    frame["signature"] = intents.sign_intent(frame, key)
    return frame


@pytest.mark.anyio
async def test_ws_market_buy_nonce_idempotent_end_to_end(client, register_soul):
    register_soul("ws_seller", essence=100.0)
    register_soul("ws_buyer", essence=500.0)
    listing_id = _list_via_rest(client, "ws_seller", 30.0)

    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-mkt-1",
            "market_buy",
            "ws_buyer",
            listing_id=listing_id,
            buyer_soul_id="ws_buyer",
        )
        ws.send_json(frame)
        ack1 = ws.receive_json()
        assert ack1["type"] == "intent_ack"
        assert ack1["status"] == "accepted"

        ws.send_json(frame)
        ack2 = ws.receive_json()
        assert ack2 == ack1

        assert _essence("ws_buyer") == 470.0

        WorldTick().pump_intents()

        ws.send_json(frame)
        ack3 = ws.receive_json()
        assert ack3["intent_id"] == ack1["intent_id"]
        assert ack3["status"] == "adjudicated"
        assert ack3["result"]["tax_collected"] == 0.6

    assert _essence("ws_buyer") == 470.0
    assert _essence("ws_seller") == 129.4
    assert _fund() == 0.6
    with database.get_db() as conn:
        n_debits = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE entry_type = 'debit'"
        ).fetchone()["c"]
        n_intents = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE kind = 'market_buy'"
        ).fetchone()["c"]
    assert n_intents == 1
    assert n_debits == 1


@pytest.mark.anyio
async def test_ws_market_buy_insufficient_funds_error(client, register_soul):
    register_soul("wse_seller", essence=100.0)
    register_soul("wse_buyer", essence=5.0)
    listing_id = _list_via_rest(client, "wse_seller", 30.0)

    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-mkt-poor",
            "market_buy",
            "wse_buyer",
            listing_id=listing_id,
            buyer_soul_id="wse_buyer",
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "INSUFFICIENT_FUNDS"
    assert _essence("wse_buyer") == 5.0


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
                "position) VALUES "
                "('crash_seller', 'o_s', 'o_s', 100.0, '[0, 0]'), "
                "('crash_buyer', 'o_b', 'o_b', 500.0, '[0, 0]')"
            )
            conn.execute(
                "INSERT INTO marketplace (listing_id, seller_id, seller_name, "
                "item, price, timestamp) VALUES "
                "('crash_lst', 'crash_seller', 'S', '{\"name\": \"Orb\"}', "
                "100.0, 1.0)"
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


async def _ws_buy_ack(port: int, nonce: str) -> dict:
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws/op1",
        additional_headers={"X-Hub-Secret": HUB_SECRET},
    ) as ws:
        connected = json.loads(await ws.recv())
        assert connected["type"] == "connected"
        snapshot = json.loads(await ws.recv())
        assert snapshot["type"] == "snapshot"
        frame = {
            "v": 1,
            "type": "intent",
            "kind": "market_buy",
            "nonce": nonce,
            "session_id": connected["session_id"],
            "soul_id": "crash_buyer",
            "listing_id": "crash_lst",
            "buyer_soul_id": "crash_buyer",
        }
        frame["signature"] = intents.sign_intent(frame, connected["hmac_key"])
        await ws.send(json.dumps(frame))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "intent_ack"
        assert ack["status"] == "accepted"
        return ack


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
    db_path = str(tmp_path / "crash1.db")
    _seed_crash_db(db_path)
    port = _free_port()

    proc1, log1 = _start_server(db_path, port, False, str(tmp_path / "s1.log"))
    try:
        await asyncio.to_thread(_wait_health, port)
        ack = await _ws_buy_ack(port, "nonce-crash-1")
        intent_id = ack["intent_id"]
        with _swapped_db(db_path):
            status, _ = _intent_status(intent_id)
            assert status == "pending"
            assert _essence("crash_buyer") == 400.0
    finally:
        proc1.kill()
        proc1.wait(timeout=15)
        log1.close()

    port2 = _free_port()
    proc2, log2 = _start_server(db_path, port2, True, str(tmp_path / "s2.log"))
    try:
        await asyncio.to_thread(_wait_health, port2)
        status, result = await asyncio.to_thread(
            _wait_intent_settled, db_path, intent_id
        )
        assert status == "adjudicated", result
        with _swapped_db(db_path):
            assert _essence("crash_buyer") == 400.0
            assert _essence("crash_seller") == 198.0
            assert _fund() == 2.0
            rows = _ledger_rows(intent_id)
            assert [r["entry_type"] for r in rows] == ["debit", "credit", "tax"]
            assert [r["amount"] for r in rows] == [100.0, 98.0, 2.0]
            assert _escrow_statuses() == [
                {"intent_id": intent_id, "status": "applied", "amount": 100.0}
            ]
            with database.get_db() as conn:
                assert (
                    conn.execute("SELECT COUNT(*) AS c FROM marketplace").fetchone()[
                        "c"
                    ]
                    == 0
                )
    finally:
        proc2.kill()
        proc2.wait(timeout=15)
        log2.close()


@pytest.mark.anyio
async def test_crash_with_rejected_outcome_refunds_escrow(tmp_path):
    db_path = str(tmp_path / "crash2.db")
    _seed_crash_db(db_path)
    port = _free_port()

    proc1, log1 = _start_server(db_path, port, False, str(tmp_path / "s1.log"))
    try:
        await asyncio.to_thread(_wait_health, port)
        ack = await _ws_buy_ack(port, "nonce-crash-2")
        intent_id = ack["intent_id"]
        with _swapped_db(db_path):
            assert _essence("crash_buyer") == 400.0
            with database.get_db() as conn:
                conn.execute("DELETE FROM marketplace WHERE listing_id = 'crash_lst'")
                conn.commit()
    finally:
        proc1.kill()
        proc1.wait(timeout=15)
        log1.close()

    port2 = _free_port()
    proc2, log2 = _start_server(db_path, port2, True, str(tmp_path / "s2.log"))
    try:
        await asyncio.to_thread(_wait_health, port2)
        status, result = await asyncio.to_thread(
            _wait_intent_settled, db_path, intent_id
        )
        assert status == "rejected", result
        assert result["reason"] == "listing_gone"
        with _swapped_db(db_path):
            assert _essence("crash_buyer") == 500.0
            assert _essence("crash_seller") == 100.0
            assert _fund() == 0.0
            assert _ledger_rows(intent_id) == []
            assert _escrow_statuses() == [
                {"intent_id": intent_id, "status": "released", "amount": 100.0}
            ]
    finally:
        proc2.kill()
        proc2.wait(timeout=15)
        log2.close()
