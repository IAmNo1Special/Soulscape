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


def test_souls_overwrite(client: TestClient):
    # First batch
    client.post(
        "/souls",
        json={"owner_id": "user1", "souls": [{"soul_id": "1", "name": "A"}]},
    )

    # Second batch (should overwrite for this owner)
    client.post(
        "/souls",
        json={"owner_id": "user1", "souls": [{"soul_id": "2", "name": "B"}]},
    )

    response = client.get("/souls")
    data = response.json()
    assert len(data) == 1
    assert data[0]["soul_id"] == "2"
    assert data[0]["name"] == "B"


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

    # Check that these fields came back as parsed objects, not strings
    assert soul["position"] == [10, 20]
    assert soul["hometown"] == {"x": 1, "y": 1}
    assert soul["orb_color"] == [255, 0, 0]
    assert soul["aura_color"] == [0, 255, 0]
