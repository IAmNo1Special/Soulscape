from fastapi.testclient import TestClient

from .. import database


def _seed_inventory(soul_id, item_name, qty=10):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO soul_inventory (soul_id, item_name, quantity) "
            "VALUES (?, ?, ?)",
            (soul_id, item_name, qty),
        )
        conn.commit()


def test_full_user_journey(client: TestClient, register_soul):
    register_soul(
        "seller_01", essence=500.0, name="Seller",
        secret="seller_secret_key_123"
    )
    register_soul(
        "buyer_01", essence=500.0, name="Buyer",
        secret="buyer_secret_key_1234"
    )
    _seed_inventory("seller_01", "Rare Candy")

    list_resp = client.post(
        "/marketplace/list",
        json={
            "seller_id": "seller_01",
            "seller_name": "Seller",
            "item": {"name": "Rare Candy", "type": "consumable"},
            "price": 100.0,
        },
        headers={"X-Hub-Secret": "seller_secret_key_123"},
    )
    assert list_resp.status_code == 200
    listing_id = list_resp.json()["listing_id"]

    marketplace = client.get(
        "/marketplace", headers={"X-Hub-Secret": "seller_secret_key_123"}
    )
    assert marketplace.status_code == 200
    listings = marketplace.json()["listings"]
    assert len(listings) == 1
    assert listings[0]["price"] == 100.0

    buy_resp = client.post(
        f"/marketplace/buy/{listing_id}",
        json={"buyer_id": "buyer_01"},
        headers={"X-Hub-Secret": "buyer_secret_key_1234"},
    )
    assert buy_resp.status_code == 200
    data = buy_resp.json()
    assert data["status"] == "success"
    assert data["tax_collected"] == 2.0
    assert data["seller_credited"] == 98.0

    marketplace = client.get(
        "/marketplace", headers={"X-Hub-Secret": "seller_secret_key_123"}
    )
    assert marketplace.status_code == 200
    assert len(marketplace.json()["listings"]) == 0

    post_resp = client.post(
        "/social/post",
        json={
            "author_id": "buyer_01",
            "author_name": "Buyer",
            "title": "Hello",
            "content": "Just bought a Rare Candy!",
        },
        headers={"X-Hub-Secret": "buyer_secret_key_1234"},
    )
    assert post_resp.status_code == 200
    message_id = post_resp.json()["message_id"]

    reply_resp = client.post(
        "/social/reply",
        json={
            "message_id": message_id,
            "author_id": "seller_01",
            "author_name": "Seller",
            "content": "Enjoy the candy!",
        },
        headers={"X-Hub-Secret": "seller_secret_key_123"},
    )
    assert reply_resp.status_code == 200
    assert reply_resp.json()["status"] == "success"

    social = client.get(
        "/social", headers={"X-Hub-Secret": "seller_secret_key_123"}
    )
    posts = social.json()
    assert len(posts) == 1
    assert posts[0]["author_name"] == "Buyer"
    assert len(posts[0]["replies"]) == 1
    assert posts[0]["replies"][0]["author_name"] == "Seller"
