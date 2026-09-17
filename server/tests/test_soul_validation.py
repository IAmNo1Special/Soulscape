import time

from fastapi.testclient import TestClient

from .. import database


def _post(client: TestClient, owner_id: str, souls: list) -> dict:
    res = client.post("/souls", json={"owner_id": owner_id, "souls": souls})
    assert res.status_code == 200
    return res.json()


def _stored(soul_id: str) -> dict:
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM souls WHERE soul_id = ?", (soul_id,))
        return dict(cursor.fetchone())


def _backdate(soul_id: str, hours_ago: float) -> None:
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET updated_at = ? WHERE soul_id = ?",
            (time.time() - hours_ago * 3600.0, soul_id),
        )
        conn.commit()


def test_essence_ignored_for_new_soul(client: TestClient):
    _post(client, "user1", [{"soul_id": "s1", "essence": 999999.0}])
    assert _stored("s1")["essence"] == 100.0


def test_essence_preserved_not_minted(client: TestClient):
    _post(client, "user1", [{"soul_id": "s1", "essence": 100.0}])
    with database.get_db() as conn:
        conn.execute("UPDATE souls SET essence = 40.0 WHERE soul_id = 's1'")
        conn.commit()
    _post(client, "user1", [{"soul_id": "s1", "essence": 999999.0}])
    assert _stored("s1")["essence"] == 40.0


def test_ranges_clamped_not_rejected(client: TestClient):
    body = _post(
        client,
        "user1",
        [
            {
                "soul_id": "s1",
                "satiety": 999.0,
                "hydration": -5.0,
                "hp": 50000.0,
                "max_hp": 50000.0,
                "level": 150,
                "stat_hp_iv": 99,
                "stat_atk_ev": 999,
                "stat_hp_base": 9999,
            }
        ],
    )
    assert body["count"] == 1
    assert body["skipped"] == []
    row = _stored("s1")
    assert row["satiety"] == 100.0
    assert row["hydration"] == 0.0
    assert row["max_hp"] == 9999.0
    assert row["hp"] == 9999.0
    assert row["level"] == 100
    assert row["stat_hp_iv"] == 31
    assert row["stat_atk_ev"] == 255
    assert row["stat_hp_base"] == 255


def test_xp_and_level_cannot_decrease(client: TestClient):
    _post(client, "user1", [{"soul_id": "s1", "level": 10, "xp": 5000}])
    body = _post(client, "user1", [{"soul_id": "s1", "level": 5, "xp": 100}])
    assert body["count"] == 0
    assert len(body["skipped"]) == 1
    row = _stored("s1")
    assert row["level"] == 10
    assert row["xp"] == 5000


def test_level_jump_too_fast_rejected(client: TestClient):
    _post(client, "user1", [{"soul_id": "s1", "level": 10, "xp": 5000}])
    body = _post(client, "user1", [{"soul_id": "s1", "level": 50, "xp": 6000}])
    assert body["count"] == 0
    assert _stored("s1")["level"] == 10


def test_slow_level_gain_accepted(client: TestClient):
    _post(client, "user1", [{"soul_id": "s1", "level": 10, "xp": 5000}])
    _backdate("s1", hours_ago=10.0)
    body = _post(client, "user1", [{"soul_id": "s1", "level": 15, "xp": 6000}])
    assert body["count"] == 1
    row = _stored("s1")
    assert row["level"] == 15
    assert row["xp"] == 6000


def test_missing_soul_id_skipped(client: TestClient):
    body = _post(client, "user1", [{"name": "Nameless"}])
    assert body["count"] == 0
    assert len(body["skipped"]) == 1


def test_malformed_position_defaults(client: TestClient):
    body = _post(client, "user1", [{"soul_id": "s1", "position": "moon"}])
    assert body["count"] == 1
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT position FROM souls WHERE soul_id = 's1'")
        import json

        # Newborn souls materialize at the origin Commons center
        # (issue #19), overriding any posted position.
        from .. import plots

        assert json.loads(cursor.fetchone()["position"]) == list(
            plots.commons_center()
        )


def test_inventory_quantities_clamped(client: TestClient):
    _post(
        client,
        "user1",
        [
            {
                "soul_id": "s1",
                "inventory": {
                    "items": [
                        {"name": "Berry", "quantity": 9999},
                        {"name": "X" * 200, "quantity": 2},
                        {"quantity": 3},
                        "junk",
                    ]
                },
            }
        ],
    )
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT item_name, quantity FROM soul_inventory WHERE soul_id = 's1'"
        )
        items = {row["item_name"]: row["quantity"] for row in cursor.fetchall()}
    assert items["Berry"] == 99
    assert items["X" * 64] == 2
    assert len(items) == 2
