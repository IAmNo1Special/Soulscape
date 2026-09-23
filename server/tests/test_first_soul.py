"""Free first-soul grant + manual-mint enforcement (online spawn rules).

Manual soul spawn is offline-only. Online custodians cannot
introduce souls by hand: the first new soul from a soulless
custodian is their free first, an empty first contact mints a
server-built one (WS hello or POST), and later manual souls are
skipped. Operators keep full powers.
"""

import pytest
from fastapi.testclient import TestClient

from .. import database
from .. import dormancy
from ..main import app


@pytest.fixture
def tamer_c():
    c = TestClient(app)
    r = c.post(
        "/tamers/register",
        json={"username": "tamer_c", "password": "s3cur3pass"},
    )
    assert r.status_code == 201
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login",
        json={"username": "tamer_c", "password": "s3cur3pass"},
    )
    assert r.status_code == 200
    return {"tamer_id": tamer_id, "token": r.json()["token"]}


def _tamer_client(token):
    return TestClient(app, headers={"X-Hub-Secret": token})


def _soul_rows(custodian_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT * FROM souls WHERE COALESCE(custodian_id, owner_id) = ?",
            (custodian_id,),
        ).fetchall()


def test_tamer_first_soul_accepted_second_rejected(tamer_c):
    tc = _tamer_client(tamer_c["token"])
    custodian = tamer_c["tamer_id"]
    first = tc.post(
        "/souls",
        json={"custodian_id": custodian, "souls": [{"soul_id": "free1"}]},
    )
    assert first.status_code == 200
    assert first.json()["count"] == 1
    assert first.json()["free_soul"] is None
    second = tc.post(
        "/souls",
        json={"custodian_id": custodian, "souls": [{"soul_id": "bought1"}]},
    )
    assert second.status_code == 200
    assert second.json()["count"] == 0
    assert second.json()["skipped"] == [
        {"soul_id": "bought1", "reason": "manual_mint_disabled"}
    ]
    rows = _soul_rows(custodian)
    assert [row["soul_id"] for row in rows] == ["free1"]


def test_empty_post_mints_free_soul_once(client):
    res = client.post("/souls", json={"custodian_id": "fresh1", "souls": []})
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 0
    granted = body["free_soul"]
    assert granted["soul_id"]
    assert granted["secret"]
    rows = _soul_rows("fresh1")
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["soul_id"] == granted["soul_id"]
    assert row["name"] == "Soul 1"
    assert float(row["essence"]) == pytest.approx(dormancy.STARTER_GRANT)
    assert database.verify_secret_hash(granted["secret"], row["secret_hash"])
    with database.get_db() as conn:
        mint = conn.execute(
            "SELECT * FROM ledger WHERE entry_type = 'mint' AND soul_id = ?",
            (granted["soul_id"],),
        ).fetchone()
    assert mint is not None
    assert float(mint["amount"]) == pytest.approx(dormancy.STARTER_GRANT)
    again = client.post("/souls", json={"custodian_id": "fresh1", "souls": []})
    assert again.status_code == 200
    assert again.json()["free_soul"] is None
    assert len(_soul_rows("fresh1")) == 1


def test_operator_new_souls_still_allowed(client):
    res = client.post(
        "/souls",
        json={
            "custodian_id": "op_managed",
            "souls": [{"soul_id": "m1"}, {"soul_id": "m2"}],
        },
    )
    assert res.status_code == 200
    assert res.json()["count"] == 2
    assert res.json()["skipped"] == []
    assert len(_soul_rows("op_managed")) == 2


def test_ws_hello_mints_free_soul(client):
    with client.websocket_connect("/ws/hello_fresh") as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
    rows = _soul_rows("hello_fresh")
    assert len(rows) == 1
    assert rows[0]["name"] == "Soul 1"
    with client.websocket_connect("/ws/hello_fresh") as ws:
        assert ws.receive_json()["type"] == "connected"
    assert len(_soul_rows("hello_fresh")) == 1


def test_ws_hello_leaves_existing_souls_alone(client, register_soul):
    register_soul("kept1", essence=100.0, name="Kept")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET owner_id = 'hello_kept', custodian_id = 'hello_kept' "
            "WHERE soul_id = 'kept1'"
        )
        conn.commit()
    with client.websocket_connect("/ws/hello_kept") as ws:
        assert ws.receive_json()["type"] == "connected"
    rows = _soul_rows("hello_kept")
    assert [row["soul_id"] for row in rows] == ["kept1"]
