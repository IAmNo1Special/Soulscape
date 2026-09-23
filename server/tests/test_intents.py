import json
import math

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from .. import database
from .. import intents
from ..world_tick import (
    INTENT_HORIZON_SECONDS,
    INTENT_MOVE_SPEED,
    WorldTick,
)


def _place_soul(soul_id, x, y, custodian_id=None):
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET position = ?, custodian_id = ? WHERE soul_id = ?",
            (json.dumps([x, y]), custodian_id, soul_id),
        )
        conn.commit()


def _soul_state(soul_id):
    from .. import persistence

    with database.get_db() as conn:
        row = conn.execute(
            "SELECT position, velocity, move_target FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
        position = json.loads(row["position"])
        velocity = json.loads(row["velocity"] or "[0, 0]")
        target = json.loads(row["move_target"]) if row["move_target"] else None
    unflushed = persistence.dirty_get(soul_id)
    if unflushed is not None:
        if unflushed.get("position") is not None:
            position = [float(unflushed["position"][0]), float(unflushed["position"][1])]
        if unflushed.get("velocity") is not None:
            velocity = [float(unflushed["velocity"][0]), float(unflushed["velocity"][1])]
        if "move_target" in unflushed:
            raw = unflushed["move_target"]
            target = None if raw is None else [float(raw[0]), float(raw[1])]
    return (position, velocity, target)


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


@contextmanager
def _open_ws(client, path="/ws/op1"):
    with client.websocket_connect(path) as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        assert connected["session_id"]
        assert connected["hmac_key"]
        assert ws.receive_json()["type"] == "snapshot"
        yield ws, connected


def test_enqueue_is_idempotent():
    first = intents.enqueue_intent(
        "sess1", "n1", "tam1", "s1", "move_to", {"x": 1.0, "y": 2.0}
    )
    second = intents.enqueue_intent(
        "sess1", "n1", "tam1", "s1", "move_to", {"x": 9.0, "y": 9.0}
    )
    assert first["intent_id"] == second["intent_id"]
    assert second["status"] == "pending"
    with database.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE session_id = ? AND nonce = ?",
            ("sess1", "n1"),
        ).fetchone()["c"]
    assert count == 1


def test_nonce_unique_per_session():
    intents.enqueue_intent("sessA", "n1", None, "s1", "move_to", {"x": 1.0})
    other = intents.enqueue_intent("sessB", "n1", None, "s1", "move_to", {"x": 1.0})
    assert other["session_id"] == "sessB"


def test_validate_payload_unknown_kind():
    payload, error = intents.validate_payload("teleport", {})
    assert payload is None
    assert error == "UNKNOWN_INTENT_KIND"


def test_validate_payload_bad_coords():
    for bad in ({"x": "abc", "y": 1.0}, {"x": float("inf"), "y": 1.0}, {"x": 1.0}, {}):
        payload, error = intents.validate_payload("move_to", bad)
        assert payload is None and error == "BAD_PAYLOAD"


def test_adjudication_rejects_custody_change():
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, custodian_id, position, essence) "
            "VALUES ('cs1', 'o1', 'tam2', '[100, 100]', 100.0)"
        )
        conn.commit()
    record = intents.enqueue_intent(
        "sessX", "nX", "tam1", "cs1", "move_to", {"x": 200.0, "y": 200.0}
    )
    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
    assert row["status"] == "rejected"
    assert json.loads(row["result"])["reason"] == "custody"


def test_move_to_clamped_to_reachable_radius(monkeypatch):
    monkeypatch.setattr(database, "SCREEN_BOUNDS", (10000, 10000))
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, position, essence) "
            "VALUES ('cl1', 'o1', '[100, 100]', 100.0)"
        )
        conn.commit()
    record = intents.enqueue_intent(
        "sessC", "nC", None, "cl1", "move_to", {"x": 9000.0, "y": 9000.0}
    )
    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
    assert row["status"] == "adjudicated"
    result = json.loads(row["result"])
    assert result["clamped"] is True
    max_reach = INTENT_MOVE_SPEED * INTENT_HORIZON_SECONDS
    got = math.hypot(result["x"] - 100.0, result["y"] - 100.0)
    assert got == pytest.approx(max_reach, rel=1e-6)
    _, velocity, target = _soul_state("cl1")
    assert math.hypot(*velocity) == pytest.approx(INTENT_MOVE_SPEED, rel=1e-6)
    assert target == [result["x"], result["y"]]


def test_move_to_within_reach_not_clamped(register_soul):
    register_soul("mv1")
    _place_soul("mv1", 100.0, 100.0)
    record = intents.enqueue_intent(
        "sessM", "nM", None, "mv1", "move_to", {"x": 400.0, "y": 100.0}
    )
    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "adjudicated"
    assert result["clamped"] is False
    assert result["x"] == 400.0 and result["y"] == 100.0
    tick = WorldTick()
    for _ in range(3):
        tick.step()
    pos, velocity, target = _soul_state("mv1")
    assert pos == [400.0, 100.0]
    assert velocity == [0.0, 0.0]
    assert target is None


def test_amble_wander_move_adjudicates_at_amble_speed(register_soul):
    from ..agents import jev as jev_mod

    register_soul("am1")
    _place_soul("am1", 100.0, 100.0)
    record = intents.enqueue_intent(
        "sessA",
        "nA",
        None,
        "am1",
        "move_to",
        {"x": 400.0, "y": 100.0, "pace": "amble", "wander": True},
    )
    WorldTick().pump_intents()
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
    assert row["status"] == "adjudicated"
    result = json.loads(row["result"])
    assert result["speed"] == pytest.approx(jev_mod.JEV_AMBLE_SPEED)
    _, velocity, _ = _soul_state("am1")
    assert math.hypot(*velocity) == pytest.approx(jev_mod.JEV_AMBLE_SPEED, rel=1e-6)


def test_crash_between_ack_and_adjudication_loses_nothing(register_soul):
    register_soul("cr1")
    _place_soul("cr1", 100.0, 100.0)
    record = intents.enqueue_intent(
        "sessR", "nR", None, "cr1", "move_to", {"x": 500.0, "y": 100.0}
    )
    assert record["status"] == "pending"
    WorldTick().pump_intents()
    with database.get_db() as conn:
        status = conn.execute(
            "SELECT status FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()["status"]
    assert status == "adjudicated"
    _, velocity, target = _soul_state("cr1")
    assert target == [500.0, 100.0]
    assert velocity[0] > 0.0


@pytest.mark.anyio
async def test_ws_intent_accepted_and_adjudicated(client: TestClient, register_soul):
    register_soul("ws1")
    _place_soul("ws1", 100.0, 100.0)
    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-1",
            "move_to",
            "ws1",
            x=400.0,
            y=100.0,
        )
        ws.send_json(frame)
        ack = ws.receive_json()
        assert ack["type"] == "intent_ack"
        assert ack["intent_id"]
        assert ack["status"] == "accepted"
        assert ack["nonce"] == "nonce-1"
        WorldTick().pump_intents()
        _, velocity, target = _soul_state("ws1")
        assert target == [400.0, 100.0]
        assert velocity[0] > 0.0


@pytest.mark.anyio
async def test_ws_duplicate_nonce_is_idempotent(client: TestClient, register_soul):
    register_soul("ws2")
    _place_soul("ws2", 100.0, 100.0)
    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-dup",
            "move_to",
            "ws2",
            x=400.0,
            y=100.0,
        )
        ws.send_json(frame)
        ack1 = ws.receive_json()
        ws.send_json(frame)
        ack2 = ws.receive_json()
        assert ack1 == ack2
        assert ack1["status"] == "accepted"
        WorldTick().pump_intents()
        ws.send_json(frame)
        ack3 = ws.receive_json()
        assert ack3["intent_id"] == ack1["intent_id"]
        assert ack3["status"] == "adjudicated"
        with database.get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM intents WHERE session_id = ? AND nonce = ?",
                (connected["session_id"], "nonce-dup"),
            ).fetchone()["c"]
        assert count == 1


@pytest.mark.anyio
async def test_ws_forged_soul_rejected(client: TestClient):
    reg = client.post(
        "/tamers/register", json={"username": "forger", "password": "password123"}
    )
    assert reg.status_code == 201
    tamer_id = reg.json()["tamer_id"]
    login = client.post(
        "/tamers/login", json={"username": "forger", "password": "password123"}
    )
    token = login.json()["token"]
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, custodian_id, position, essence) "
            "VALUES ('victim', 'o9', 'other-tamer', '[100, 100]', 100.0)"
        )
        conn.commit()
    with client.websocket_connect(f"/ws/{tamer_id}?token={token}") as ws:
        connected = ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-forge",
            "move_to",
            "victim",
            x=400.0,
            y=100.0,
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "CUSTODY_DENIED"
        with database.get_db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM intents").fetchone()["c"]
        assert count == 0
        pos, _, _ = _soul_state("victim")
        assert pos == [100.0, 100.0]


@pytest.mark.anyio
async def test_ws_tamer_own_soul_accepted(client: TestClient):
    reg = client.post(
        "/tamers/register", json={"username": "owner2", "password": "password123"}
    )
    tamer_id = reg.json()["tamer_id"]
    login = client.post(
        "/tamers/login", json={"username": "owner2", "password": "password123"}
    )
    token = login.json()["token"]
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, custodian_id, position, essence) "
            "VALUES ('mine', 'o9', ?, '[100, 100]', 100.0)",
            (tamer_id,),
        )
        conn.commit()
    with client.websocket_connect(f"/ws/{tamer_id}?token={token}") as ws:
        connected = ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-own",
            "move_to",
            "mine",
            x=300.0,
            y=300.0,
        )
        ws.send_json(frame)
        ack = ws.receive_json()
        assert ack["type"] == "intent_ack"
        assert ack["status"] == "accepted"


@pytest.mark.anyio
async def test_ws_bad_signature_closes(client: TestClient, register_soul):
    register_soul("ws3")
    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-bad",
            "move_to",
            "ws3",
            x=1.0,
            y=1.0,
        )
        frame["signature"] = "0" * 64
        ws.send_json(frame)
        with pytest.raises(WebSocketDisconnect):
            for _ in range(5):
                ws.receive_json()


@pytest.mark.anyio
async def test_ws_missing_signature_closes(client: TestClient):
    with _open_ws(client) as (ws, connected):
        ws.send_json(
            {
                "v": 1,
                "type": "intent",
                "kind": "move_to",
                "nonce": "n",
                "session_id": connected["session_id"],
                "soul_id": "x",
                "x": 1.0,
                "y": 1.0,
            }
        )
        with pytest.raises(WebSocketDisconnect):
            for _ in range(5):
                ws.receive_json()


@pytest.mark.anyio
async def test_ws_unknown_kind_rejected(client: TestClient, register_soul):
    register_soul("ws4")
    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-uk",
            "teleport",
            "ws4",
            x=1.0,
            y=1.0,
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "UNKNOWN_INTENT_KIND"


@pytest.mark.anyio
async def test_ws_unknown_soul_rejected(client: TestClient):
    with _open_ws(client) as (ws, connected):
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-ns",
            "move_to",
            "nope",
            x=1.0,
            y=1.0,
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "SOUL_NOT_FOUND"


@pytest.mark.anyio
async def test_ws_soul_update_removed(client: TestClient, register_soul):
    register_soul("ws5")
    _place_soul("ws5", 100.0, 100.0)
    with _open_ws(client) as (ws, connected):
        ws.send_json(
            {
                "v": 1,
                "type": "soul_update",
                "souls": [{"soul_id": "ws5", "x": 999.0, "y": 999.0}],
            }
        )
        err = None
        for _ in range(5):
            frame = ws.receive_json()
            if frame.get("type") == "error":
                err = frame
                break
        assert err is not None
        assert err["code"] == "CHANNEL_REMOVED"
        pos, _, _ = _soul_state("ws5")
        assert pos == [100.0, 100.0]
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
