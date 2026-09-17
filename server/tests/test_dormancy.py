"""Dormancy: wallet-derived freeze state (issue #22).

A soul with essence <= 0 is dormant: frozen movement/cognition,
biology at x0.25, HP floor 1, no collapse, theft immunity, statue
rendering. Any credit crossing 0 -> >0 wakes it (soul_woke journaled,
think schedule reset).
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from shared import protocol

from .. import biology
from .. import database
from .. import dormancy
from .. import intents
from .. import market
from .. import viewport as vp
from ..world_tick import WorldTick


def _essence(soul_id: str) -> float:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
        return float(row["essence"])


def _journal_types() -> list[str]:
    with database.get_db() as conn:
        return [
            r[0]
            for r in conn.execute("SELECT type FROM journal ORDER BY seq").fetchall()
        ]


def _drain_to_zero(soul_id: str) -> None:
    """Drain a soul to 0 through the same journaled path escrow code uses."""
    with database.get_db() as conn:
        before = dormancy.cached_essence(conn, soul_id) or 0.0
        conn.execute("UPDATE souls SET essence = 0.0 WHERE soul_id = ?", (soul_id,))
        dormancy.note_essence_change(conn, soul_id, before)
        conn.commit()


def _credit(soul_id: str, amount: float) -> None:
    with database.get_db() as conn:
        before = dormancy.cached_essence(conn, soul_id) or 0.0
        conn.execute(
            "UPDATE souls SET essence = essence + ? WHERE soul_id = ?",
            (amount, soul_id),
        )
        dormancy.note_essence_change(conn, soul_id, before)
        conn.commit()


# ---------------------------------------------------------------------------
# Derivation: funded -> drain -> dormant; credit -> awake
# ---------------------------------------------------------------------------


def test_drain_to_zero_freezes_and_journals(db_conn, register_soul):
    register_soul("frost", essence=100.0)
    assert not dormancy.soul_is_dormant(db_conn, "frost")
    _drain_to_zero("frost")
    assert dormancy.is_dormant(_essence("frost"))
    assert dormancy.soul_is_dormant(db_conn, "frost")
    assert "soul_dormant" in _journal_types()


def test_partial_drain_stays_awake(db_conn, register_soul):
    register_soul("almost", essence=100.0)
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET essence = 0.01 WHERE soul_id = 'almost'"
        )
        conn.commit()
    assert not dormancy.soul_is_dormant(db_conn, "almost")
    assert "soul_dormant" not in _journal_types()


def test_credit_one_wakes_and_journals(db_conn, register_soul):
    register_soul("sleeper", essence=100.0)
    _drain_to_zero("sleeper")
    assert "soul_dormant" in _journal_types()
    _credit("sleeper", 1.0)
    assert _essence("sleeper") == 1.0
    assert not dormancy.soul_is_dormant(db_conn, "sleeper")
    assert "soul_woke" in _journal_types()


def test_wake_resets_think_schedule(db_conn, register_soul, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        dormancy, "reset_think_schedule", lambda soul_id: calls.append(soul_id)
    )
    register_soul("thinker", essence=100.0)
    _drain_to_zero("thinker")
    assert calls == []
    _credit("thinker", 5.0)
    assert calls == ["thinker"]


def test_no_flip_no_journal(db_conn, register_soul):
    register_soul("steady", essence=100.0)
    _credit("steady", 10.0)  # awake -> awake
    assert "soul_dormant" not in _journal_types()
    assert "soul_woke" not in _journal_types()


# ---------------------------------------------------------------------------
# Frozen: movement ingress + adjudication + tick
# ---------------------------------------------------------------------------


def test_move_to_rejected_at_adjudication(db_conn, register_soul):
    register_soul("statue", essence=100.0)
    _drain_to_zero("statue")
    record = intents.enqueue_intent(
        "sess", "n-dorm", "owner_statue", "statue",
        "move_to", {"x": 500.0, "y": 500.0},
    )
    assert record["status"] == "pending"
    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
    assert row["status"] == "rejected"
    assert json.loads(row["result"])["reason"] == "soul_dormant"


def test_wake_accepts_movement(db_conn, register_soul):
    register_soul("walker", essence=100.0)
    _drain_to_zero("walker")
    _credit("walker", 10.0)
    record = intents.enqueue_intent(
        "sess", "n-wake", "owner_walker", "walker",
        "move_to", {"x": 150.0, "y": 150.0},
    )
    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
    assert row["status"] == "adjudicated", row["result"]


def test_tick_freezes_position(db_conn):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) "
        "VALUES ('ice', 'o_ice', '[100.0, 200.0]', '[10.0, -5.0]', 0.0)"
    )
    db_conn.commit()
    tick = WorldTick()
    assert tick.step() == 0
    row = db_conn.execute(
        "SELECT position FROM souls WHERE soul_id = 'ice'"
    ).fetchone()
    assert json.loads(row["position"]) == [100.0, 200.0]


def test_tick_moves_funded_soul(db_conn):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) "
        "VALUES ('warm', 'o_warm', '[100.0, 200.0]', '[10.0, 0.0]', 50.0)"
    )
    db_conn.commit()
    assert WorldTick().step() == 1


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
async def test_ws_move_to_rejected_early_for_dormant(client: TestClient):
    reg = client.post(
        "/tamers/register", json={"username": "dozy_owner", "password": "password123"}
    )
    assert reg.status_code == 201
    tamer_id = reg.json()["tamer_id"]
    login = client.post(
        "/tamers/login", json={"username": "dozy_owner", "password": "password123"}
    )
    token = login.json()["token"]
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, custodian_id, position, essence) "
            "VALUES ('dozy_ws', 'o9', ?, '[100, 100]', 0.0)",
            (tamer_id,),
        )
        conn.commit()
    with client.websocket_connect(f"/ws/{tamer_id}?token={token}") as ws:
        connected = ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        frame = _intent_frame(
            connected["hmac_key"], connected["session_id"], "nonce-dozy",
            "move_to", "dozy_ws", x=400.0, y=100.0,
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "SOUL_DORMANT"
        with database.get_db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM intents").fetchone()["c"]
        assert count == 0


# ---------------------------------------------------------------------------
# Biology: x0.25, HP floor, no collapse -- live and catch-up paths
# ---------------------------------------------------------------------------


def _bio_fields(**over):
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
        "dormant": False,
    }
    base.update(over)
    return base


def test_biology_quarter_rate_pure():
    now = time.time()
    # Resting satiety: 100/36 per hour. 36 h dormant -> 25 lost, not 100.
    out = biology._decay_soul(_bio_fields(dormant=True), 36 * 3600, now)
    assert out["satiety"] == pytest.approx(75.0)
    out = biology._decay_soul(_bio_fields(), 36 * 3600, now)
    assert out["satiety"] == pytest.approx(0.0)
    # Hydration likewise quartered.
    out = biology._decay_soul(_bio_fields(dormant=True), 24 * 3600, now)
    assert out["hydration"] == pytest.approx(75.0)


def test_biology_quarter_rate_catchup_path(db_conn):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, satiety, hydration, hp, max_hp,"
        " state, essence, position, velocity) VALUES "
        "('d1', 'o1', 100.0, 100.0, 100.0, 100.0, 'normal', 0.0,"
        " '[0,0]', '[0,0]')"
    )
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, satiety, hydration, hp, max_hp,"
        " state, essence, position, velocity) VALUES "
        "('a1', 'o1', 100.0, 100.0, 100.0, 100.0, 'normal', 50.0,"
        " '[0,0]', '[0,0]')"
    )
    db_conn.commit()
    now = time.time()
    biology.apply_biology_decay(db_conn, 12 * 3600, now, 1)
    db_conn.commit()
    rows = {
        r["soul_id"]: dict(r)
        for r in db_conn.execute("SELECT soul_id, satiety FROM souls")
    }
    # 12 h: awake loses 100/36*12 = 33.33; dormant loses a quarter of that.
    assert rows["a1"]["satiety"] == pytest.approx(100.0 - 100.0 / 3.0)
    assert rows["d1"]["satiety"] == pytest.approx(100.0 - 100.0 / 12.0)


def test_biology_quarter_rate_live_path(db_conn):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, satiety, hydration, hp, max_hp,"
        " state, essence, position, velocity) VALUES "
        "('d2', 'o1', 100.0, 100.0, 100.0, 100.0, 'normal', 0.0,"
        " '[0,0]', '[0,0]')"
    )
    db_conn.commit()
    now = time.time()
    # One live tick = 50 * 0.2 s = 10 s of decay.
    biology.apply_biology_tick(db_conn, 1, now, 0.2)
    row = db_conn.execute(
        "SELECT satiety FROM souls WHERE soul_id = 'd2'"
    ).fetchone()
    assert row["satiety"] == pytest.approx(100.0 - (100.0 / 36.0) * 0.25 * (10.0 / 3600.0))


def test_starving_dormant_hp_floor_no_collapse(db_conn):
    now = time.time()
    out = biology._decay_soul(
        _bio_fields(dormant=True, satiety=0.0, hp=1.0), 72 * 3600, now
    )
    assert out.get("hp", 1.0) == pytest.approx(1.0)
    assert out.get("state", "normal") == "normal"
    assert "_journal" not in out


def test_dormant_hp_below_one_floored(db_conn):
    now = time.time()
    out = biology._decay_soul(
        _bio_fields(dormant=True, satiety=0.0, hp=0.4), 3600, now
    )
    assert out["hp"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Theft hook (future)
# ---------------------------------------------------------------------------


def test_can_be_stolen_from(db_conn, register_soul):
    register_soul("rich", essence=100.0)
    register_soul("broke", essence=100.0)
    assert dormancy.can_be_stolen_from(db_conn, "rich") is True
    _drain_to_zero("broke")
    assert dormancy.can_be_stolen_from(db_conn, "broke") is False
    # Unknown soul: fail closed.
    assert dormancy.can_be_stolen_from(db_conn, "ghost") is False


# ---------------------------------------------------------------------------
# Birth: 100 starter grant + mint row, no remint on update
# ---------------------------------------------------------------------------


def test_newborn_grant_and_mint(client: TestClient):
    res = client.post(
        "/souls",
        json={
            "owner_id": "mom",
            "souls": [{"soul_id": "baby", "owner_id": "mom", "name": "Baby"}],
        },
    )
    assert res.status_code == 200, res.text
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = 'baby'"
        ).fetchone()
        assert float(row["essence"]) == 100.0
        assert not dormancy.soul_is_dormant(conn, "baby")
        mints = conn.execute(
            "SELECT amount FROM ledger WHERE soul_id = 'baby' "
            "AND entry_type = 'mint'"
        ).fetchall()
        assert len(mints) == 1
        assert float(mints[0]["amount"]) == 100.0


def test_update_does_not_remint(client: TestClient):
    payload = {
        "owner_id": "mom",
        "souls": [{"soul_id": "kid", "owner_id": "mom", "name": "Kid"}],
    }
    assert client.post("/souls", json=payload).status_code == 200
    payload["souls"][0]["name"] = "Kid Renamed"
    assert client.post("/souls", json=payload).status_code == 200
    with database.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE soul_id = 'kid' "
            "AND entry_type = 'mint'"
        ).fetchone()["n"]
        assert n == 1


def test_conservation_with_mint(client: TestClient, register_soul):
    register_soul("mint_seller", essence=100.0)
    register_soul("mint_buyer", essence=500.0)
    res = client.post(
        "/marketplace/list",
        json={"seller_id": "mint_seller", "seller_name": "S",
              "item": {"name": "Orb"}, "price": 50.0},
    )
    listing_id = res.json()["listing_id"]
    res = client.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_id": "mint_buyer"}
    )
    assert res.status_code == 200, res.text
    with database.get_db() as conn:
        report = market.verify_balances(conn)
    assert report["ok"], report["soul_drifts"]


# ---------------------------------------------------------------------------
# Market: dormant sellers stay listed; proceeds wake them
# ---------------------------------------------------------------------------


def test_dormant_seller_listing_stays_buyable_and_wakes(
    client: TestClient, register_soul
):
    register_soul("dozy", essence=100.0)
    register_soul("shopper", essence=500.0)
    res = client.post(
        "/marketplace/list",
        json={"seller_id": "dozy", "seller_name": "Dozy",
              "item": {"name": "Relic"}, "price": 40.0},
    )
    assert res.status_code == 200, res.text
    listing_id = res.json()["listing_id"]
    _drain_to_zero("dozy")

    # Dormant: cannot author listings...
    res = client.post(
        "/marketplace/list",
        json={"seller_id": "dozy", "seller_name": "Dozy",
              "item": {"name": "Other"}, "price": 10.0},
    )
    assert res.status_code == 400
    assert "dormant" in res.json()["detail"].lower()
    # ...nor cancel as the seller (the operator could still cancel with
    # its own authority -- tested at the enqueue guard level here).
    with pytest.raises(market.MarketRefusal) as exc_info:
        market.enqueue_market_intent(
            "sess_dz", "n_dz", "owner_dozy", "dozy",
            market.KIND_MARKET_CANCEL, {"listing_id": listing_id},
        )
    assert exc_info.value.reason == "soul_dormant"

    # ...but the existing listing stays buyable, and the proceeds wake.
    res = client.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_id": "shopper"}
    )
    assert res.status_code == 200, res.text
    assert _essence("dozy") == pytest.approx(39.2)  # 40 - 2% tax
    assert _essence("shopper") == pytest.approx(460.0)
    with database.get_db() as conn:
        assert not dormancy.soul_is_dormant(conn, "dozy")
    assert "soul_dormant" in _journal_types()
    assert "soul_woke" in _journal_types()


def test_dormant_buyer_rejected(client: TestClient, register_soul):
    register_soul("live_seller", essence=100.0)
    register_soul("flat_buyer", essence=500.0)
    res = client.post(
        "/marketplace/list",
        json={"seller_id": "live_seller", "seller_name": "S",
              "item": {"name": "Orb"}, "price": 25.0},
    )
    listing_id = res.json()["listing_id"]
    _drain_to_zero("flat_buyer")
    res = client.post(
        f"/marketplace/buy/{listing_id}", json={"buyer_id": "flat_buyer"}
    )
    assert res.status_code == 400
    assert "dormant" in res.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Social / plots: dormant souls cannot author paid intents
# ---------------------------------------------------------------------------


def test_dormant_cannot_post(client: TestClient, register_soul):
    register_soul("mute", essence=100.0)
    _drain_to_zero("mute")
    res = client.post(
        "/social/post",
        json={"author_id": "mute", "author_name": "M",
              "title": "T", "content": "nope"},
    )
    assert res.status_code == 400
    assert "dormant" in res.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Viewport: snapshot + delta carry the dormant statue state
# ---------------------------------------------------------------------------


def _vp_insert(db_conn, soul_id, essence):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, essence) "
        "VALUES (?, ?, '[10, 10]', ?)",
        (soul_id, f"owner_{soul_id}", essence),
    )
    db_conn.commit()


def test_viewport_snapshot_carries_dormancy(db_conn):
    _vp_insert(db_conn, "awake1", 50.0)
    _vp_insert(db_conn, "sleepy1", 0.0)
    manager = vp.ViewportManager()
    session = manager.create("owner1")
    frame = vp.build_snapshot(session, protocol.SnapReason.FULL_SYNC, 0)
    by_id = {s["soul_id"]: s for s in frame["souls"]}
    assert by_id["awake1"]["dormant"] is False
    assert by_id["sleepy1"]["dormant"] is True
    assert session.committed_dormancy == {"awake1": False, "sleepy1": True}


def test_viewport_delta_carries_dormancy_flip(db_conn):
    _vp_insert(db_conn, "flip", 0.0)
    manager = vp.ViewportManager()
    session = manager.create("owner1")
    vp.build_snapshot(session, protocol.SnapReason.FULL_SYNC, 0)
    _credit("flip", 10.0)
    current = vp.read_dormancy()
    ops = vp.diff_dormancy(current, session.committed_dormancy)
    assert len(ops) == 1
    op, domain = ops[0]
    assert op["domain"] == "dormancy"
    assert op["soul_id"] == "flip"
    assert op["state"]["dormant"] is False


def test_viewport_diff_dormancy_noop(db_conn):
    assert vp.diff_dormancy({"a": False}, {"a": False}) == []
    assert vp.diff_dormancy({"a": True}, {"a": True}) == []
