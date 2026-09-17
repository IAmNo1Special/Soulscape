import os
import uuid

import pytest
from fastapi.testclient import TestClient

from ..database import DB_PATH, get_db, init_db, hash_secret
from ..main import app


@pytest.fixture(scope="session", autouse=True)
def session_db():
    # Use a unique name for this session to avoid locks from previous runs
    DB_PATH = f"test_soulscape_{uuid.uuid4().hex}.db"
    init_db()
    yield
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(autouse=True)
def setup_data():
    # Clean tables instead of deleting file
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM souls")
        cursor.execute("DELETE FROM soul_inventory")
        cursor.execute("DELETE FROM messages")
        cursor.execute("DELETE FROM marketplace")

        # Add some test data (using hashed secrets)
        secret_hash_1 = hash_secret("secret_1")
        secret_prefix_1 = secret_hash_1[:16]
        secret_hash_2 = hash_secret("secret_2")
        secret_prefix_2 = secret_hash_2[:16]
        cursor.execute(
            "INSERT INTO souls (soul_id, owner_id, name, essence, secret_hash, secret_prefix) VALUES (?, ?, ?, ?, ?, ?)",
            ("soul_1", "user_1", "Test Soul 1", 100.0, secret_hash_1, secret_prefix_1),
        )
        cursor.execute(
            "INSERT INTO souls (soul_id, owner_id, name, essence, secret_hash, secret_prefix) VALUES (?, ?, ?, ?, ?, ?)",
            ("soul_2", "user_2", "Test Soul 2", 100.0, secret_hash_2, secret_prefix_2),
        )
        # Add some inventory items
        cursor.execute(
            "INSERT INTO soul_inventory (soul_id, item_name, quantity) VALUES (?, ?, ?)",
            ("soul_1", "Berry", 5),
        )
        cursor.execute(
            "INSERT INTO soul_inventory (soul_id, item_name, quantity) VALUES (?, ?, ?)",
            ("soul_2", "Herb", 3),
        )

        # Marketplace item
        cursor.execute(
            "INSERT INTO marketplace (listing_id, seller_id, seller_name, item, price, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "listing_1",
                "user_2",
                "Seller 2",
                '{"name": "Rare Candy"}',
                50.0,
                1234567.8,
            ),
        )
        conn.commit()


def test_auth_soul_secret():
    client = TestClient(app)
    # Valid soul secret
    response = client.get("/social", headers={"X-Hub-Secret": "secret_1"})
    assert response.status_code == 200

    # Invalid secret
    response = client.get("/social", headers={"X-Hub-Secret": "wrong_secret"})
    assert response.status_code == 403


def test_idor_social_post():
    client = TestClient(app)
    # user_1 tries to post as user_2
    response = client.post(
        "/social/post",
        headers={"X-Hub-Secret": "secret_1"},
        json={
            "author_id": "user_2",
            "author_name": "Impersonator",
            "title": "Hack",
            "content": "I am user 2",
            "timestamp": 1234567.8,
        },
    )
    assert response.status_code == 200
    # Verify that it was actually recorded as soul_1 (the identity id)
    # Note: identity.id for soul secret is the soul_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT author_id FROM messages WHERE title = 'Hack'")
        row = cursor.fetchone()
        assert (
            row["author_id"] == "soul_1"
        )  # The author_id was derived from the secret (the soul_id)


def test_idor_marketplace_buy():
    client = TestClient(app)
    # soul_1 tries to buy using soul_2 as buyer_id
    response = client.post(
        "/marketplace/buy/listing_1",
        headers={"X-Hub-Secret": "secret_1"},
        json={"buyer_id": "soul_2"},
    )
    assert response.status_code == 200
    # Verify that soul_1 was charged, not soul_2
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT essence FROM souls WHERE soul_id = 'soul_1'")
        assert cursor.fetchone()["essence"] == 50.0  # 100 - 50
        cursor.execute("SELECT essence FROM souls WHERE soul_id = 'soul_2'")
        assert cursor.fetchone()["essence"] == 100.0  # Unchanged


def test_operator_override():
    client = TestClient(app)
    hub_secret = os.getenv("HUB_SECRET_KEY", "test_secret")
    os.environ["HUB_SECRET_KEY"] = hub_secret

    # Operator can specify any author_id; the authoring soul pays (#17
    # precedent: the operator charge exemption is gone).
    response = client.post(
        "/social/post",
        headers={"X-Hub-Secret": hub_secret},
        json={
            "author_id": "soul_2",
            "author_name": "Admin",
            "title": "Admin Post",
            "content": "Admin content",
            "timestamp": 1234567.8,
        },
    )
    assert response.status_code == 200
    assert response.json()["cost"] == 20.0

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT author_id, author_type FROM messages WHERE title = 'Admin Post'"
        )
        row = cursor.fetchone()
        assert row["author_id"] == "soul_2"
        assert row["author_type"] == "soul"
        cursor.execute("SELECT essence FROM souls WHERE soul_id = 'soul_2'")
        assert cursor.fetchone()["essence"] == 80.0


def test_operator_reply_cost():
    client = TestClient(app)
    hub_secret = os.getenv("HUB_SECRET_KEY", "test_secret")

    # Pre-requisite: post exists
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages (message_id, parent_id, author_type, "
            "author_id, author_name, title, body, created_at) "
            "VALUES ('msg_1', NULL, 'soul', 'soul_1', 'n1', 't1', 'c1', 1.0)"
        )
        conn.commit()

    # Operator replying as a soul charges that soul (#17 precedent: no
    # operator exemption).
    response = client.post(
        "/social/reply",
        headers={"X-Hub-Secret": hub_secret},
        json={
            "message_id": "msg_1",
            "author_id": "soul_1",
            "author_name": "Admin",
            "content": "Admin reply",
        },
    )
    assert response.status_code == 200
    assert response.json()["cost"] == 8.0
    with get_db() as conn:
        essence = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = 'soul_1'"
        ).fetchone()["essence"]
        assert essence == 92.0


def test_souls_inventory_optimization():
    client = TestClient(app)
    # Fetch souls for user_1
    response = client.get(
        "/souls?owner_id=user_1", headers={"X-Hub-Secret": "secret_1"}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["soul_id"] == "soul_1"
    assert "Berry" in data[0]["inventory"]
    assert (
        "Herb" not in data[0]["inventory"]
    )  # Should NOT be in the map if filtered correctly


def test_secret_redaction():
    client = TestClient(app)
    response = client.get("/souls", headers={"X-Hub-Secret": "secret_1"})
    assert response.status_code == 200
    data = response.json()
    for soul in data:
        assert "secret" not in soul
        assert "secret_hash" not in soul
        assert "secret_prefix" not in soul


def test_idor_get_souls():
    client = TestClient(app)
    # user_1 tries to query for user_2
    response = client.get(
        "/souls?owner_id=user_2", headers={"X-Hub-Secret": "secret_1"}
    )
    # Our mitigation forces owner_id to identity.id for users, or forbids if mismatch.
    # In my implementation, I forbad if mismatch.
    assert response.status_code == 403


def test_token_persistence():
    client = TestClient(app)
    # Check current secret hash
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT secret_hash FROM souls WHERE soul_id = 'soul_1'")
        old_hash = cursor.fetchone()["secret_hash"]
        assert old_hash is not None

    # Perform update for soul_1
    response = client.post(
        "/souls",
        headers={"X-Hub-Secret": "secret_1"},
        json={
            "owner_id": "user_1",
            "souls": [
                {
                    "soul_id": "soul_1",
                    "name": "Updated Soul 1",
                    "essence": 150.0,
                }
            ],
        },
    )
    assert response.status_code == 200

    # Verify secret hash still exists
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT secret_hash FROM souls WHERE soul_id = 'soul_1'")
        new_hash = cursor.fetchone()["secret_hash"]
        assert new_hash == old_hash


def test_idor_social_delete():
    client = TestClient(app)
    # Pre-requisite: user_2 post exists
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages (message_id, parent_id, author_type, "
            "author_id, author_name, title, body, created_at) "
            "VALUES ('msg_u2', NULL, 'soul', 'soul_2', 'User 2', 't', "
            "'U2 Post', 1.0)"
        )
        conn.commit()

    # user_1 tries to delete user_2's post by passing soul_2 as author_id
    response = client.post(
        "/social/delete/msg_u2",
        headers={"X-Hub-Secret": "secret_1"},
        json={"author_id": "soul_2"},
    )
    # Should fail because logic strictly uses identity.id (soul_1)
    assert response.status_code == 404
    assert response.json()["detail"] == "Message not found or unauthorized"


def test_idor_social_edit():
    client = TestClient(app)
    # user_1 tries to edit user_2's post
    response = client.post(
        "/social/edit/msg_u2",
        headers={"X-Hub-Secret": "secret_1"},
        json={"content": "Hacked", "author_id": "soul_2"},
    )
    assert response.status_code == 404


def test_idor_marketplace_list():
    client = TestClient(app)
    # user_1 tries to list item as user_2
    response = client.post(
        "/marketplace/list",
        headers={"X-Hub-Secret": "secret_1"},
        json={
            "seller_id": "user_2",
            "seller_name": "User 1 pretending",
            "item": {"name": "Fake Item"},
            "price": 10.0,
        },
    )
    assert response.status_code == 200
    listing_id = response.json()["listing_id"]

    # Verify that it was recorded with seller_id 'soul_1' (the identity ID)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT seller_id FROM marketplace WHERE listing_id = ?",
            (listing_id,),
        )
        assert cursor.fetchone()["seller_id"] == "soul_1"