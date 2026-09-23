import asyncio
import hashlib
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from .. import database
from .. import main
from ..security import UserIdentity, require_scoped


def _public_client() -> TestClient:
    return TestClient(main.app)


def _tamer_client(token: str) -> TestClient:
    return TestClient(main.app, headers={"X-Hub-Secret": token})


@pytest.fixture
def tamer_a():
    c = _public_client()
    r = c.post(
        "/tamers/register",
        json={"username": "tamer_a", "password": "s3cur3pass"},
    )
    assert r.status_code == 201
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login",
        json={"username": "tamer_a", "password": "s3cur3pass"},
    )
    assert r.status_code == 200
    return {"tamer_id": tamer_id, "token": r.json()["token"]}


@pytest.fixture
def tamer_b():
    c = _public_client()
    r = c.post(
        "/tamers/register",
        json={"username": "tamer_b", "password": "an0therpass"},
    )
    assert r.status_code == 201
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login",
        json={"username": "tamer_b", "password": "an0therpass"},
    )
    assert r.status_code == 200
    return {"tamer_id": tamer_id, "token": r.json()["token"]}


def test_register_login_logout_roundtrip():
    c = _public_client()
    r = c.post(
        "/tamers/register",
        json={"username": "ash", "password": "pikachu123"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["tamer_id"].startswith("tmr_")
    assert body["username"] == "ash"

    r = c.post("/tamers/login", json={"username": "ash", "password": "pikachu123"})
    assert r.status_code == 200
    session = r.json()
    assert session["token"].startswith("tms_")
    assert session["tamer_id"] == body["tamer_id"]
    assert session["expires_at"] > time.time()

    tc = _tamer_client(session["token"])
    r = tc.get("/souls")
    assert r.status_code == 200
    assert r.json() == []

    r = tc.post("/tamers/logout")
    assert r.status_code == 200

    r = tc.get("/souls")
    assert r.status_code == 403


def test_login_wrong_password_rejected():
    c = _public_client()
    c.post("/tamers/register", json={"username": "ash", "password": "pikachu123"})
    r = c.post("/tamers/login", json={"username": "ash", "password": "wrongpass1"})
    assert r.status_code == 401
    r = c.post("/tamers/login", json={"username": "nobody", "password": "whatever12"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid username or password"


def test_register_duplicate_username_rejected():
    c = _public_client()
    r = c.post("/tamers/register", json={"username": "ash", "password": "pikachu123"})
    assert r.status_code == 201
    r = c.post("/tamers/register", json={"username": "ash", "password": "otherpass1"})
    assert r.status_code == 409


def test_register_validation():
    c = _public_client()
    r = c.post("/tamers/register", json={"username": "ab", "password": "pikachu123"})
    assert r.status_code == 400
    r = c.post(
        "/tamers/register", json={"username": "bad name!", "password": "pikachu123"}
    )
    assert r.status_code == 400
    r = c.post("/tamers/register", json={"username": "ash", "password": "short"})
    assert r.status_code == 400


def test_revoked_session_rejected_server_side(tamer_a):
    tc = _tamer_client(tamer_a["token"])
    assert tc.get("/souls").status_code == 200
    assert tc.post("/tamers/logout").status_code == 200
    assert tc.get("/souls").status_code == 403
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT revoked FROM tamer_sessions WHERE tamer_id = ?",
            (tamer_a["tamer_id"],),
        ).fetchone()
    assert row["revoked"] == 1


def test_expired_session_rejected(tamer_a):
    token_hash = hashlib.sha256(tamer_a["token"].encode()).hexdigest()
    with database.get_db() as conn:
        conn.execute(
            "UPDATE tamer_sessions SET expires_at = ? WHERE session_hash = ?",
            (time.time() - 1, token_hash),
        )
        conn.commit()
    tc = _tamer_client(tamer_a["token"])
    assert tc.get("/souls").status_code == 403


def test_password_stored_as_argon2id_and_token_only_hashed(tamer_a):
    with database.get_db() as conn:
        tamer = conn.execute(
            "SELECT password_hash FROM tamers WHERE tamer_id = ?",
            (tamer_a["tamer_id"],),
        ).fetchone()
        assert tamer["password_hash"].startswith("$argon2id$")
        sessions = conn.execute(
            "SELECT session_hash FROM tamer_sessions WHERE tamer_id = ?",
            (tamer_a["tamer_id"],),
        ).fetchall()
    assert len(sessions) == 1
    assert (
        sessions[0]["session_hash"]
        == hashlib.sha256(tamer_a["token"].encode()).hexdigest()
    )
    assert tamer_a["token"] not in sessions[0]["session_hash"]


def test_tamer_soul_custody_roundtrip(tamer_a):
    tc = _tamer_client(tamer_a["token"])
    r = tc.post(
        "/souls",
        json={
            "custodian_id": tamer_a["tamer_id"],
            "souls": [{"soul_id": "s1", "name": "Pikachu"}],
        },
    )
    assert r.status_code == 200
    r = tc.get("/souls")
    assert r.status_code == 200
    souls = r.json()
    assert len(souls) == 1
    assert souls[0]["custodian_id"] == tamer_a["tamer_id"]
    assert souls[0]["owner_id"] == tamer_a["tamer_id"]


def test_legacy_owner_id_accepted_during_migration(tamer_a):
    tc = _tamer_client(tamer_a["token"])
    r = tc.post(
        "/souls",
        json={"owner_id": tamer_a["tamer_id"], "souls": [{"soul_id": "s1"}]},
    )
    assert r.status_code == 200
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT owner_id, custodian_id FROM souls WHERE soul_id = 's1'"
        ).fetchone()
    assert row["owner_id"] == tamer_a["tamer_id"]
    assert row["custodian_id"] == tamer_a["tamer_id"]
    r = tc.get("/souls", params={"owner_id": tamer_a["tamer_id"]})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_cross_custody_post_denied(tamer_a, tamer_b):
    tc = _tamer_client(tamer_a["token"])
    r = tc.post(
        "/souls",
        json={
            "custodian_id": tamer_b["tamer_id"],
            "souls": [{"soul_id": "s1"}],
        },
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Cross-custody access denied"


def test_cross_custody_get_denied(tamer_a, tamer_b):
    tc = _tamer_client(tamer_a["token"])
    r = tc.get("/souls", params={"custodian_id": tamer_b["tamer_id"]})
    assert r.status_code == 403


def test_operator_cross_custody_allowed(client):
    r = client.post(
        "/souls",
        json={"custodian_id": "someone_else", "souls": [{"soul_id": "s1"}]},
    )
    assert r.status_code == 200
    r = client.get("/souls", params={"custodian_id": "someone_else"})
    assert r.status_code == 200
    assert r.json()[0]["custodian_id"] == "someone_else"


def test_missing_custodian_rejected(tamer_a):
    tc = _tamer_client(tamer_a["token"])
    r = tc.post("/souls", json={"souls": [{"soul_id": "s1"}]})
    assert r.status_code == 400


def test_null_safe_lineage_on_creation(tamer_a):
    tc = _tamer_client(tamer_a["token"])
    r = tc.post(
        "/souls",
        json={
            "custodian_id": tamer_a["tamer_id"],
            "souls": [
                {"soul_id": "child1", "mother_id": None, "father_id": ""},
                {
                    "soul_id": "child2",
                    "mother_id": "mom1",
                    "father_id": "dad1",
                },
            ],
        },
    )
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["skipped"] == [
        {"soul_id": "child2", "reason": "manual_mint_disabled"}
    ]
    with database.get_db() as conn:
        c1 = conn.execute(
            "SELECT mother_id, father_id FROM souls WHERE soul_id = 'child1'"
        ).fetchone()
        c2 = conn.execute(
            "SELECT soul_id FROM souls WHERE soul_id = 'child2'"
        ).fetchone()
    assert c1["mother_id"] is None
    assert c1["father_id"] is None
    assert c2 is None


def test_tamer_websocket_auth(tamer_a):
    c = _public_client()
    with c.websocket_connect(
        f"/ws/{tamer_a['tamer_id']}?token={tamer_a['token']}"
    ) as ws:
        data = ws.receive_json()
        assert data["type"] == "connected"


def test_require_scoped_denies_scopeless_identity():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_scoped(UserIdentity(id="x", role="user")))
    assert exc.value.status_code == 403
