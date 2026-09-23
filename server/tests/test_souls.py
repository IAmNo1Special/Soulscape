from fastapi.testclient import TestClient


def test_get_souls_empty(client: TestClient):
    response = client.get("/souls")
    assert response.status_code == 200
    assert response.json() == []


def test_update_and_get_souls(client: TestClient):
    souls_payload = [
        {
            "soul_id": "1001",
            "owner_id": "user1",
            "name": "FireSpirit",
            "species": "Elemental",
            "level": 5,
            "hp": 80.0,
            "inventory": {"items": [{"name": "Coal", "quantity": 10}]},
        },
        {
            "soul_id": "1002",
            "owner_id": "user1",
            "name": "WaterDrop",
            "species": "Elemental",
            "level": 3,
            "hp": 50.0,
        },
    ]
    payload = {"owner_id": "user1", "souls": souls_payload}
    response = client.post("/souls", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    # Verify souls and inventory
    response = client.get("/souls")
    data = response.json()
    assert len(data) == 2

    # Check FireSpirit
    spirit = next(s for s in data if s["soul_id"] == "1001")
    assert spirit["name"] == "FireSpirit"
    assert spirit["inventory"]["Coal"] == 10

    # Check WaterDrop
    drop = next(s for s in data if s["soul_id"] == "1002")
    assert drop["name"] == "WaterDrop"
    assert drop["inventory"] == {}


def test_souls_merge_not_overwrite(client: TestClient):
    # First batch
    client.post(
        "/souls",
        json={"owner_id": "user1", "souls": [{"soul_id": "1", "name": "A"}]},
    )

    # Second batch merges: souls not mentioned are kept (omission is
    # never deletion -- syncs routinely carry partial knowledge).
    client.post(
        "/souls",
        json={"owner_id": "user1", "souls": [{"soul_id": "2", "name": "B"}]},
    )

    response = client.get("/souls")
    data = response.json()
    assert {s["soul_id"] for s in data} == {"1", "2"}

    # Re-saving a known soul updates it in place.
    client.post(
        "/souls",
        json={"owner_id": "user1", "souls": [{"soul_id": "1", "name": "A2"}]},
    )
    response = client.get("/souls")
    data = response.json()
    assert {s["soul_id"] for s in data} == {"1", "2"}
    assert next(s for s in data if s["soul_id"] == "1")["name"] == "A2"


def test_get_souls_filter(client: TestClient):
    # Create souls for two owners
    client.post(
        "/souls",
        json={
            "owner_id": "user1",
            "souls": [{"soul_id": "1", "name": "A"}],
        },
    )
    client.post(
        "/souls",
        json={
            "owner_id": "user2",
            "souls": [{"soul_id": "2", "name": "B"}],
        },
    )

    # Filter for user1
    res1 = client.get("/souls?owner_id=user1")
    data1 = res1.json()
    assert len(data1) == 1
    assert data1[0]["soul_id"] == "1"

    # Filter for user2
    res2 = client.get("/souls?owner_id=user2")
    data2 = res2.json()
    assert len(data2) == 1
    assert data2[0]["soul_id"] == "2"


def test_souls_json_parsing(client: TestClient):
    # Send complex data that should be JSON encoded/decoded
    client.post(
        "/souls",
        json={
            "owner_id": "user1",
            "souls": [
                {
                    "soul_id": "3",
                    "name": "C",
                    "position": [10, 20],
                    "hometown": {"x": 1, "y": 1},
                    "orb_color": [255, 0, 0],
                    "aura_color": [0, 255, 0],
                }
            ],
        },
    )

    response = client.get("/souls")
    data = response.json()
    soul = data[0]

    # Check that these fields came back as parsed objects, not strings.
    # Newborn souls materialize at the origin Commons center (issue #19),
    # so the posted position is overridden for a new soul.
    from .. import plots

    assert soul["position"] == list(plots.commons_center())
    assert soul["hometown"] == {"x": 1, "y": 1}
    assert soul["orb_color"] == [255, 0, 0]
    assert soul["aura_color"] == [0, 255, 0]


def test_post_souls_reports_actual_spawn_positions(client: TestClient):
    from .. import plots

    commons = list(plots.commons_center())
    response = client.post(
        "/souls",
        json={
            "owner_id": "user1",
            "souls": [
                {"soul_id": "born1", "name": "Newb", "position": [999, 999]},
                {"soul_id": "born2", "name": "Newb2"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["count"] == 2
    assert body["skipped"] == []
    reported = {s["soul_id"]: s["position"] for s in body["souls"]}
    assert reported["born1"] == [float(commons[0]), float(commons[1])]
    assert reported["born2"] == [float(commons[0]), float(commons[1])]

    stored = {s["soul_id"]: s["position"] for s in client.get("/souls").json()}
    assert stored["born1"] == reported["born1"]
    assert stored["born2"] == reported["born2"]


def test_post_souls_reports_existing_soul_position(client: TestClient):
    client.post(
        "/souls",
        json={"owner_id": "user1", "souls": [{"soul_id": "old1", "name": "A"}]},
    )
    response = client.post(
        "/souls",
        json={
            "owner_id": "user1",
            "souls": [{"soul_id": "old1", "name": "A", "position": [42, 7]}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    reported = {s["soul_id"]: s["position"] for s in body["souls"]}
    assert reported["old1"] == [42.0, 7.0]
    stored = {s["soul_id"]: s["position"] for s in client.get("/souls").json()}
    assert stored["old1"] == reported["old1"]
