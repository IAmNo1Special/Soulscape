from fastapi.testclient import TestClient

from .. import database


def _essence(soul_id: str) -> float:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
        return float(row["essence"])


def _post(
    client: TestClient,
    secret: str,
    author_id: str = "1",
    author_name: str = "Alice",
    title: str = "Hello World",
    content: str = "This is a social post.",
) -> dict:
    res = client.post(
        "/social/post",
        json={
            "author_id": author_id,
            "author_name": author_name,
            "title": title,
            "content": content,
        },
        headers={"X-Hub-Secret": secret},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_tree_emits_legacy_aliases(client: TestClient, register_soul):
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret_key_123")
    _post(client, "alice_secret_key_123")
    node = client.get("/social").json()[0]
    assert node["content"] == node["body"] == "This is a social post."
    assert node["timestamp"] == node["created_at"]


def test_get_social_empty(client: TestClient):
    response = client.get("/social")
    assert response.status_code == 200
    assert response.json() == []


def test_create_post(client: TestClient, register_soul):
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret_key_123")
    data = _post(client, "alice_secret_key_123")
    assert data["status"] == "success"
    assert "message_id" in data
    assert data["cost"] == 20.0
    assert _essence("1") == 80.0

    response = client.get("/social", headers={"X-Hub-Secret": "alice_secret_key_123"})
    posts = response.json()
    assert len(posts) == 1
    node = posts[0]
    assert node["message_id"] == data["message_id"]
    assert node["parent_id"] is None
    assert node["author_type"] == "soul"
    assert node["author_id"] == "1"
    assert node["author_name"] == "Alice"
    assert node["title"] == "Hello World"
    assert node["body"] == "This is a social post."
    assert node["deleted"] is False
    assert node["replies"] == []


def test_create_post_requires_title(client: TestClient, register_soul):
    register_soul("1", essence=100.0, secret="alice_secret_key_123")
    res = client.post(
        "/social/post",
        json={"author_id": "1", "author_name": "Alice", "content": "no title"},
        headers={"X-Hub-Secret": "alice_secret_key_123"},
    )
    assert res.status_code == 400
    assert _essence("1") == 100.0
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_reply_to_post(client: TestClient, register_soul):
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret_key_123")
    register_soul("2", essence=100.0, name="Bob", secret="bob_secret_key_1234")

    message_id = _post(client, "alice_secret_key_123", title="First post")["message_id"]

    reply_payload = {
        "message_id": message_id,
        "author_id": "2",
        "author_name": "Bob",
        "content": "Nice post!",
    }
    response = client.post(
        "/social/reply",
        json=reply_payload,
        headers={"X-Hub-Secret": "bob_secret_key_1234"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["cost"] == 8.0
    assert _essence("2") == 92.0

    response = client.get("/social", headers={"X-Hub-Secret": "alice_secret_key_123"})
    posts = response.json()
    assert len(posts[0]["replies"]) == 1
    reply = posts[0]["replies"][0]
    assert reply["message_id"] == data["reply_id"]
    assert reply["parent_id"] == message_id
    assert reply["author_name"] == "Bob"
    assert reply["body"] == "Nice post!"
    assert reply["title"] is None
    assert reply["replies"] == []


def test_reply_to_nonexistent_post(client: TestClient, register_soul):
    register_soul("2", essence=100.0, secret="bob_secret_key_1234")
    reply_payload = {
        "message_id": "none",
        "author_id": "2",
        "author_name": "Bob",
        "content": "Ghost reply",
    }
    response = client.post(
        "/social/reply",
        json=reply_payload,
        headers={"X-Hub-Secret": "bob_secret_key_1234"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Target message none not found"
    assert _essence("2") == 100.0


def test_edit_message(client: TestClient, register_soul):
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret_key_123")
    message_id = _post(client, "alice_secret_key_123")["message_id"]

    edit_res = client.post(
        f"/social/edit/{message_id}",
        json={"author_id": "1", "content": "Edited content"},
        headers={"X-Hub-Secret": "alice_secret_key_123"},
    )
    assert edit_res.status_code == 200
    assert _essence("1") == 80.0

    get_res = client.get("/social", headers={"X-Hub-Secret": "alice_secret_key_123"})
    posts = get_res.json()
    assert posts[0]["body"] == "Edited content"
    assert posts[0]["title"] == "Hello World"
    assert posts[0]["edited_at"] is not None

    register_soul("2", secret="bob_secret_key_1234")
    fail_res = client.post(
        f"/social/edit/{message_id}",
        json={"author_id": "2", "content": "Hacker edit"},
        headers={"X-Hub-Secret": "bob_secret_key_1234"},
    )
    assert fail_res.status_code == 404
    assert fail_res.json()["detail"] == "Message not found or unauthorized"


def test_delete_message(client: TestClient, register_soul):
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret_key_123")
    message_id = _post(client, "alice_secret_key_123")["message_id"]

    register_soul("2", secret="bob_secret_key_1234")
    fail_res = client.post(
        f"/social/delete/{message_id}",
        json={"author_id": "2"},
        headers={"X-Hub-Secret": "bob_secret_key_1234"},
    )
    assert fail_res.status_code == 404

    get_res = client.get("/social", headers={"X-Hub-Secret": "alice_secret_key_123"})
    assert len(get_res.json()) == 1
    assert get_res.json()[0]["deleted"] is False

    del_res = client.post(
        f"/social/delete/{message_id}",
        json={"author_id": "1"},
        headers={"X-Hub-Secret": "alice_secret_key_123"},
    )
    assert del_res.status_code == 200
    assert _essence("1") == 80.0

    # Soft delete: the node stays as a tombstone.
    get_res = client.get("/social", headers={"X-Hub-Secret": "alice_secret_key_123"})
    posts = get_res.json()
    assert len(posts) == 1
    assert posts[0]["deleted"] is True
    assert posts[0]["body"] == "[deleted]"


def test_operator_delete(client: TestClient, register_soul, hub_secret):
    register_soul("1", essence=100.0, name="Alice")
    message_id = _post(
        client,
        hub_secret,
        author_id="1",
        content="To be deleted by Admin",
    )["message_id"]

    del_res = client.post(
        f"/social/delete/{message_id}",
        json={"author_id": "999"},
    )
    assert del_res.status_code == 200

    get_res = client.get("/social")
    posts = get_res.json()
    assert len(posts) == 1
    assert posts[0]["deleted"] is True

    with database.get_db() as conn:
        row = conn.execute(
            "SELECT action FROM audit_log WHERE target_id = ?",
            (message_id,),
        ).fetchone()
        assert row is not None
        assert row["action"] == "social_delete_as_operator"
