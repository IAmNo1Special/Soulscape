from unittest.mock import patch


def test_marketplace_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Connection Fail")):
        response = client.get("/marketplace")
        assert response.status_code == 500
        assert "DB Connection Fail" in response.json()["detail"]


def test_add_listing_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post(
            "/marketplace/list",
            json={
                "seller_id": "1",
                "seller_name": "A",
                "item": {},
                "price": 10,
            },
        )
        assert response.status_code == 500


def test_buy_item_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post("/marketplace/buy/123", json={"buyer_id": "1"})
        assert response.status_code == 500


def test_get_social_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.get("/social")
        assert response.status_code == 500


def test_create_post_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post(
            "/social/post",
            json={"author_id": "1", "author_name": "A", "content": "C"},
        )
        assert response.status_code == 500


def test_reply_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post(
            "/social/reply",
            json={
                "message_id": "1",
                "author_id": "1",
                "author_name": "A",
                "content": "C",
            },
        )
        assert response.status_code == 500


def test_edit_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post(
            "/social/edit/1", json={"author_id": "1", "content": "C"}
        )
        assert response.status_code == 500


def test_delete_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post("/social/delete/1", json={"author_id": "1"})
        assert response.status_code == 500


def test_get_souls_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.get("/souls")
        assert response.status_code == 500


def test_update_souls_error(client):
    with patch("server.database.get_db", side_effect=Exception("DB Error")):
        response = client.post("/souls", json={"owner_id": "u1", "souls": []})
        assert response.status_code == 500
