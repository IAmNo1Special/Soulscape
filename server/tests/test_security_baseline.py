import hashlib
import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from .. import database
from ..main import app
from ..rate_limit import SlidingWindowLimiter


def _public() -> TestClient:
    return TestClient(app)


def _tamer_client(token: str) -> TestClient:
    return TestClient(app, headers={"X-Hub-Secret": token})


@pytest.fixture
def rl_tamer():
    c = _public()
    r = c.post(
        "/tamers/register",
        json={"username": "rl_tamer", "password": "rlpass1234"},
    )
    assert r.status_code == 201
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login",
        json={"username": "rl_tamer", "password": "rlpass1234"},
    )
    assert r.status_code == 200
    token = r.json()["token"]
    tc = _tamer_client(token)
    r = tc.post(
        "/souls",
        json={
            "custodian_id": tamer_id,
            "souls": [{"soul_id": tamer_id, "name": "Rate Soul"}],
        },
    )
    assert r.status_code == 200
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET essence = 100000 WHERE soul_id = ?",
            (tamer_id,),
        )
        conn.commit()
    return {"tamer_id": tamer_id, "token": token, "client": tc}


def test_no_plaintext_secrets_at_rest_and_index_used(client, register_soul):
    register_soul("sec9", secret="supersecret9_xxxx")
    with database.get_db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(souls)").fetchall()]
        assert "secret" not in cols
        row = conn.execute(
            "SELECT secret_hash, secret_prefix FROM souls WHERE soul_id = 'sec9'"
        ).fetchone()
        assert row["secret_hash"].startswith("$argon2id$")
        assert "supersecret9" not in row["secret_hash"]
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT soul_id FROM souls WHERE secret_prefix = ?",
            (row["secret_prefix"],),
        ).fetchall()
    assert any("idx_souls_secret_prefix" in str(step[3]) for step in plan), [
        tuple(s) for s in plan
    ]


def test_ws_ticket_mint_and_connect(client):
    r = client.post("/ws/ticket")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"].startswith("wst_")
    assert body["expires_in"] == 60
    with database.get_db() as conn:
        rows = conn.execute("SELECT ticket_hash FROM ws_tickets").fetchall()
    assert len(rows) == 1
    assert rows[0]["ticket_hash"] == hashlib.sha256(body["ticket"].encode()).hexdigest()

    with client.websocket_connect(f"/ws/HUB_OPERATOR?token={body['ticket']}") as ws:
        assert ws.receive_json()["type"] == "connected"

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/HUB_OPERATOR?token={body['ticket']}"):
            pass
    assert exc.value.code == 1008


def test_ws_ticket_expiry(client):
    r = client.post("/ws/ticket")
    ticket = r.json()["ticket"]
    with database.get_db() as conn:
        conn.execute(
            "UPDATE ws_tickets SET expires_at = ?",
            (time.time() - 1,),
        )
        conn.commit()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/HUB_OPERATOR?token={ticket}"):
            pass
    assert exc.value.code == 1008


def test_ws_raw_soul_secret_rejected(client, register_soul):
    register_soul("wssec", secret="wssecret99_xxxxxx")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/owner_wssec?token=wssecret99_xxxxxx"):
            pass
    assert exc.value.code == 1008


def test_rotated_secret_rejected_and_ws_sessions_revoked(client, register_soul):
    register_soul("rot9", secret="oldsecret99_xxxx")
    owner = "owner_rot9"
    session_id = database.create_ws_session(owner, "rot9", "k" * 32)
    r = client.post(
        "/souls",
        json={
            "owner_id": owner,
            "souls": [{"soul_id": "rot9", "secret": "newsecret99_xxxx"}],
        },
    )
    assert r.status_code == 200
    assert database.get_ws_session(session_id) is None

    c = _public()
    r = c.post("/ws/ticket", headers={"X-Hub-Secret": "oldsecret99_xxxx"})
    assert r.status_code == 403
    r = c.post("/ws/ticket", headers={"X-Hub-Secret": "newsecret99_xxxx"})
    assert r.status_code == 200


def test_login_rate_limit_boundary():
    c = _public()
    c.post(
        "/tamers/register",
        json={"username": "rl_login", "password": "rlpass1234"},
    )
    for _ in range(10):
        r = c.post(
            "/tamers/login",
            json={"username": "rl_login", "password": "wrongpass12"},
        )
        assert r.status_code == 401
    r = c.post(
        "/tamers/login",
        json={"username": "rl_login", "password": "rlpass1234"},
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_social_write_rate_limit_boundary(rl_tamer):
    tc = rl_tamer["client"]
    for i in range(6):
        r = tc.post(
            "/social/post",
            json={
                "author_name": "RL",
                "author_id": rl_tamer["tamer_id"],
                "content": f"post {i}",
            },
        )
        assert r.status_code == 200, r.text
    r = tc.post(
        "/social/post",
        json={
            "author_id": rl_tamer["tamer_id"],
            "author_name": "RL",
            "content": "post 7",
        },
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_market_write_rate_limit_boundary(rl_tamer):
    tc = rl_tamer["client"]
    for i in range(30):
        r = tc.post(
            "/marketplace/list",
            json={
                "item": {"name": f"item{i}"},
                "price": 1.0,
                "seller_id": rl_tamer["tamer_id"],
                "seller_name": "RL",
            },
        )
        assert r.status_code == 200, r.text
    r = tc.post(
        "/marketplace/list",
        json={
            "item": {"name": "x"},
            "price": 1.0,
            "seller_id": rl_tamer["tamer_id"],
            "seller_name": "RL",
        },
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_read_rate_limit_boundary(rl_tamer):
    tc = rl_tamer["client"]
    for _ in range(120):
        r = tc.get("/souls")
        assert r.status_code == 200
        time.sleep(0.02)
    r = tc.get("/souls")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_sliding_window_boundaries_and_burst():
    limiter = SlidingWindowLimiter()
    for _ in range(5):
        allowed, _ = limiter.check("k1", 5, 60)
        assert allowed
    allowed, retry_after = limiter.check("k1", 5, 60)
    assert not allowed
    assert retry_after > 0

    burst = SlidingWindowLimiter()
    results = [burst.check("k2", 3, 60)[0] for _ in range(10)]
    assert results == [True, True, True] + [False] * 7

    assert burst.check("other-key", 3, 60)[0]


def test_sliding_window_slides():
    limiter = SlidingWindowLimiter()
    assert limiter.check("k3", 1, 0.05)[0]
    assert not limiter.check("k3", 1, 0.05)[0]
    time.sleep(0.06)
    assert limiter.check("k3", 1, 0.05)[0]
