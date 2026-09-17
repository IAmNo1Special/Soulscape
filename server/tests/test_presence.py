"""Tests for the privacy-gated tamer presence pipeline (issue #28).

Covers: strict schema rejection (REST 422 + WS intent rejection), the
tamer-only identity rule (operators cannot spoof), durable enqueue +
tick adjudication, heartbeat staleness marking, the tamer_return ->
#25 escalation trigger, and tamer_presence in think observations.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from .. import database
from .. import intents
from .. import presence as presence_module
from .. import main as main_module
from ..agents import deliberation, pool as pool_module, reflex
from ..world_tick import WorldTick


def _register_tamer(client: TestClient, username: str) -> dict:
    reg = client.post(
        "/tamers/register",
        json={"username": username, "password": "s3cur3pass!"},
    )
    assert reg.status_code == 201
    tamer_id = reg.json()["tamer_id"]
    login = client.post(
        "/tamers/login",
        json={"username": username, "password": "s3cur3pass!"},
    )
    assert login.status_code == 200
    token = login.json()["token"]
    return {"tamer_id": tamer_id, "token": token}


def _tamer_client(token: str) -> TestClient:
    return TestClient(main_module.app, headers={"X-Hub-Secret": token})


def _insert_soul(soul_id: str, custodian_id: str) -> None:
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, custodian_id, position, "
            "essence, satiety, hydration, hp, max_hp) "
            "VALUES (?, ?, ?, '[0, 0]', 100.0, 100.0, 100.0, 100.0, 100.0)",
            (soul_id, custodian_id, custodian_id),
        )
        conn.commit()


class TestStrictSchema:
    def test_valid_minimal(self):
        payload, error = presence_module.validate_presence_payload(
            {"presence": "active", "idle_bucket": "0-5"}
        )
        assert error is None
        assert payload == {"presence": "active", "idle_bucket": "0-5"}

    def test_valid_full(self):
        payload, error = presence_module.validate_presence_payload(
            {
                "presence": "idle",
                "idle_bucket": "30+",
                "event": "tamer_return",
                "app_category": "game",
            }
        )
        assert error is None
        assert payload["app_category"] == "game"

    def test_unknown_field_rejected(self):
        _, error = presence_module.validate_presence_payload(
            {
                "presence": "active",
                "idle_bucket": "0-5",
                "window_title": "Banking - https://evil.example",
            }
        )
        assert error is not None and "UNKNOWN_FIELDS" in error

    def test_exact_timestamp_field_rejected(self):
        _, error = presence_module.validate_presence_payload(
            {
                "presence": "active",
                "idle_bucket": "0-5",
                "last_input_at": 1758086400.123,
            }
        )
        assert error is not None

    def test_bad_presence_rejected(self):
        _, error = presence_module.validate_presence_payload(
            {"presence": "typing", "idle_bucket": "0-5"}
        )
        assert error == "BAD_PRESENCE"

    def test_bad_bucket_rejected(self):
        _, error = presence_module.validate_presence_payload(
            {"presence": "idle", "idle_bucket": "45"}
        )
        assert error == "BAD_IDLE_BUCKET"

    def test_bad_event_rejected(self):
        _, error = presence_module.validate_presence_payload(
            {"presence": "idle", "idle_bucket": "5-30", "event": "keystroke"}
        )
        assert error == "BAD_EVENT"

    def test_bad_category_rejected(self):
        _, error = presence_module.validate_presence_payload(
            {
                "presence": "active",
                "idle_bucket": "0-5",
                "app_category": "https://evil.example",
            }
        )
        assert error == "BAD_APP_CATEGORY"

    def test_missing_required_rejected(self):
        _, error = presence_module.validate_presence_payload({"presence": "active"})
        assert error == "BAD_IDLE_BUCKET"


class TestRestIngress:
    def test_report_accepted_and_stored(self, client: TestClient):
        tamer = _register_tamer(client, "pres_a")
        tc = _tamer_client(tamer["token"])
        r = tc.post(
            "/presence/report",
            json={"presence": "active", "idle_bucket": "0-5"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tamer_id"] == tamer["tamer_id"]
        assert body["presence"] == "active"
        state = presence_module.get_presence(tamer["tamer_id"])
        assert state is not None and state["presence"] == "active"

    def test_padded_payload_422(self, client: TestClient):
        tamer = _register_tamer(client, "pres_b")
        tc = _tamer_client(tamer["token"])
        r = tc.post(
            "/presence/report",
            json={
                "presence": "active",
                "idle_bucket": "0-5",
                "window_title": "x",
            },
        )
        assert r.status_code == 422

    def test_unknown_enum_422(self, client: TestClient):
        tamer = _register_tamer(client, "pres_c")
        tc = _tamer_client(tamer["token"])
        r = tc.post(
            "/presence/report",
            json={"presence": "hyperactive", "idle_bucket": "0-5"},
        )
        assert r.status_code == 422

    def test_app_category_without_opt_in_422(self, client: TestClient):
        tamer = _register_tamer(client, "pres_d")
        tc = _tamer_client(tamer["token"])
        r = tc.post(
            "/presence/report",
            json={
                "presence": "active",
                "idle_bucket": "0-5",
                "app_category": "game",
            },
        )
        assert r.status_code == 422

    def test_app_category_with_opt_in_accepted(self, client: TestClient):
        tamer = _register_tamer(client, "pres_e")
        tc = _tamer_client(tamer["token"])
        opt = tc.put("/presence/app-opt-in", json={"enabled": True})
        assert opt.status_code == 200
        r = tc.post(
            "/presence/report",
            json={
                "presence": "active",
                "idle_bucket": "0-5",
                "app_category": "game",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["app_category"] == "game"

    def test_opt_in_toggle_off_rejects_category(self, client: TestClient):
        tamer = _register_tamer(client, "pres_f")
        tc = _tamer_client(tamer["token"])
        assert tc.put("/presence/app-opt-in", json={"enabled": True}).status_code == 200
        assert (
            tc.put("/presence/app-opt-in", json={"enabled": False}).status_code == 200
        )
        r = tc.post(
            "/presence/report",
            json={
                "presence": "active",
                "idle_bucket": "0-5",
                "app_category": "game",
            },
        )
        assert r.status_code == 422

    def test_operator_cannot_report_presence(self, client: TestClient):
        r = client.post(
            "/presence/report",
            json={"presence": "active", "idle_bucket": "0-5"},
        )
        assert r.status_code == 403

    def test_read_own_presence(self, client: TestClient):
        tamer = _register_tamer(client, "pres_g")
        tc = _tamer_client(tamer["token"])
        assert tc.get("/presence").json()["presence"] is None
        tc.post("/presence/report", json={"presence": "locked", "idle_bucket": "5-30"})
        body = tc.get("/presence").json()
        assert body["presence"] == "locked"
        assert body["idle_bucket"] == "5-30"


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
async def test_ws_presence_accepted_and_adjudicated(client: TestClient):
    reg = client.post(
        "/tamers/register", json={"username": "wpres_a", "password": "password123"}
    )
    tamer_id = reg.json()["tamer_id"]
    login = client.post(
        "/tamers/login", json={"username": "wpres_a", "password": "password123"}
    )
    token = login.json()["token"]
    with client.websocket_connect(f"/ws/{tamer_id}?token={token}") as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        assert ws.receive_json()["type"] == "snapshot"
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-pres-1",
            "tamer_presence",
            "ignored",
            presence="idle",
            idle_bucket="5-30",
        )
        ws.send_json(frame)
        ack = ws.receive_json()
        assert ack["type"] == "intent_ack"
        assert ack["status"] == "accepted"
    WorldTick().pump_intents()
    state = presence_module.get_presence(tamer_id)
    assert state is not None
    assert state["presence"] == "idle"
    assert state["idle_bucket"] == "5-30"


@pytest.mark.anyio
async def test_ws_padded_presence_rejected(client: TestClient):
    reg = client.post(
        "/tamers/register", json={"username": "wpres_b", "password": "password123"}
    )
    tamer_id = reg.json()["tamer_id"]
    login = client.post(
        "/tamers/login", json={"username": "wpres_b", "password": "password123"}
    )
    token = login.json()["token"]
    with client.websocket_connect(f"/ws/{tamer_id}?token={token}") as ws:
        connected = ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-pres-2",
            "tamer_presence",
            "ignored",
            presence="active",
            idle_bucket="0-5",
            screenshot="aGVsbG8=",
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "BAD_PAYLOAD" in err["code"]
    assert presence_module.get_presence(tamer_id) is None


@pytest.mark.anyio
async def test_ws_operator_presence_rejected(client: TestClient):
    with client.websocket_connect("/ws/op1") as ws:
        connected = ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        frame = _intent_frame(
            connected["hmac_key"],
            connected["session_id"],
            "nonce-pres-3",
            "tamer_presence",
            "ignored",
            presence="active",
            idle_bucket="0-5",
        )
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "CUSTODY_DENIED"


class TestAdjudicationAndStaleness:
    def test_adjudication_stores_and_marks_adjudicated(self, db_conn):
        now = time.time()
        record = presence_module.enqueue_presence_intent(
            "sess-1",
            "nonce-adj-1",
            "tmr_x",
            {"presence": "locked", "idle_bucket": "5-30", "event": "lock"},
        )
        assert record["status"] == "pending"
        WorldTick().pump_intents()
        fresh = intents.get_intent_by_nonce("sess-1", "nonce-adj-1")
        assert fresh["status"] == "adjudicated"
        state = presence_module.get_presence("tmr_x", now)
        assert state["presence"] == "locked"
        assert state["last_event"] == "lock"
        assert state["stale"] is False

    def test_staleness_marks_away_after_three_missed_beats(self, db_conn):
        now = time.time()
        presence_module.record_presence(
            "tmr_y", {"presence": "active", "idle_bucket": "0-5"}, now
        )
        fresh = presence_module.get_presence(
            "tmr_y", now + presence_module.STALENESS_SECONDS - 1
        )
        assert fresh is not None and fresh["stale"] is False
        stale = presence_module.get_presence(
            "tmr_y", now + presence_module.STALENESS_SECONDS + 1
        )
        assert stale is not None
        assert stale["stale"] is True
        assert stale["presence"] == "away"
        assert stale["idle_bucket"] == "30+"

    def test_unknown_tamer_reads_none(self, db_conn):
        assert presence_module.get_presence("tmr_nobody") is None

    def test_tamer_return_fires_escalation(self, db_conn):
        tamer_id = "tmr_ret"
        _insert_soul("soul_ret", tamer_id)
        old = time.time() - presence_module.TAMER_RETURN_ABSENCE_SECONDS - 60
        presence_module.record_presence(
            tamer_id, {"presence": "idle", "idle_bucket": "30+"}, old
        )
        presence_module.enqueue_presence_intent(
            "sess-2",
            "nonce-adj-2",
            tamer_id,
            {"presence": "active", "idle_bucket": "0-5", "event": "tamer_return"},
        )
        WorldTick().pump_intents()
        fresh = intents.get_intent_by_nonce("sess-2", "nonce-adj-2")
        assert fresh["status"] == "adjudicated"
        assert fresh["result"]["tamer_return"] is True
        assert (
            deliberation.tracker().should_escalate("soul_ret", time.time())
            == "tamer_return"
        )

    def test_no_return_escalation_without_long_absence(self, db_conn):
        tamer_id = "tmr_noret"
        _insert_soul("soul_noret", tamer_id)
        presence_module.record_presence(
            tamer_id, {"presence": "idle", "idle_bucket": "5-30"}, time.time() - 60
        )
        presence_module.enqueue_presence_intent(
            "sess-3",
            "nonce-adj-3",
            tamer_id,
            {"presence": "active", "idle_bucket": "0-5"},
        )
        WorldTick().pump_intents()
        fresh = intents.get_intent_by_nonce("sess-3", "nonce-adj-3")
        assert fresh["result"]["tamer_return"] is False
        assert deliberation.tracker().should_escalate("soul_noret", time.time()) is None

    def test_revalidation_at_adjudication_rejects_smuggled_fields(self, db_conn):
        intents.enqueue_intent(
            "sess-4",
            "nonce-adj-4",
            "tmr_z",
            "soul_z",
            "tamer_presence",
            {
                "presence": "active",
                "idle_bucket": "0-5",
                "window_title": "smuggled",
            },
        )
        WorldTick().pump_intents()
        fresh = intents.get_intent_by_nonce("sess-4", "nonce-adj-4")
        assert fresh["status"] == "rejected"
        assert presence_module.get_presence("tmr_z") is None


class TestThinkObservation:
    def _think_pool(self):

        p = pool_module.AgentPool()
        return p

    def test_presence_reaches_think_within_heartbeat(self, db_conn):
        from .. import world as world_mod

        tamer_id = "tmr_think"
        _insert_soul("soul_think", tamer_id)
        presence_module.record_presence(
            tamer_id,
            {"presence": "idle", "idle_bucket": "5-30"},
            time.time(),
        )
        vision = world_mod.WorldVision()
        vision.rebuild()
        p = self._think_pool()
        result = asyncio.run(
            p.think("soul_think", vision, reflex.NullProvider(), 0, time.time())
        )
        assert result["status"] == "thought"
        observed = p.last_observation("soul_think")
        assert observed is not None
        tp = observed["tamer_presence"]
        assert tp is not None
        assert tp["presence"] == "idle"
        assert tp["idle_bucket"] == "5-30"

    def test_unknown_presence_is_null_never_fabricated(self, db_conn):
        from .. import world as world_mod

        _insert_soul("soul_think2", "tmr_unknown")
        vision = world_mod.WorldVision()
        vision.rebuild()
        p = self._think_pool()
        asyncio.run(
            p.think("soul_think2", vision, reflex.NullProvider(), 0, time.time())
        )
        observed = p.last_observation("soul_think2")
        assert observed is not None
        assert observed["tamer_presence"] is None
