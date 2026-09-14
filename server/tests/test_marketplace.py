from fastapi.testclient import TestClient


def test_get_marketplace_empty(client: TestClient):
    response = client.get("/marketplace")
    assert response.status_code == 200
    data = response.json()
    assert data["essence_fund"] == 0.0
    assert data["listings"] == []


def test_add_listing(client: TestClient, register_soul):
    register_soul("123", name="Test Seller")
    listing_payload = {
        "seller_id": "123",
        "seller_name": "Test Seller",
        "item": {"name": "Magic Orb", "rarity": "Legendary"},
        "price": 100.0,
    }
    response = client.post("/marketplace/list", json=listing_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "listing_id" in data

    # Verify listing is now in marketplace
    response = client.get("/marketplace")
    listings = response.json()["listings"]
    assert len(listings) == 1
    assert listings[0]["seller_name"] == "Test Seller"
    assert listings[0]["item"]["name"] == "Magic Orb"


def test_buy_item(client: TestClient, register_soul):
    # Add a listing first
    register_soul("123", name="Test Seller")
    register_soul("456", essence=500.0, name="Test Buyer")

    listing_payload = {
        "seller_id": "123",
        "seller_name": "Test Seller",
        "item": {"name": "Old Boot"},
        "price": 50.0,
    }
    res_list = client.post("/marketplace/list", json=listing_payload)
    listing_id = res_list.json()["listing_id"]

    # Buy the item
    buyer_data = {"buyer_id": "456"}
    response = client.post(f"/marketplace/buy/{listing_id}", json=buyer_data)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["item"]["name"] == "Old Boot"
    assert data["tax_collected"] == 50.0 * 0.02

    # Verify listing is removed and essence fund increased
    response = client.get("/marketplace")
    data = response.json()
    assert len(data["listings"]) == 0
    assert data["essence_fund"] == 50.0 * 0.02


def test_buy_item_not_found(client: TestClient, register_soul):
    register_soul("456")
    response = client.post("/marketplace/buy/nonexistent", json={"buyer_id": "456"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Listing not found"
