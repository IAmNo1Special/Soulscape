from fastapi.testclient import TestClient


def test_get_social_empty(client: TestClient):
    response = client.get("/social")
    assert response.status_code == 200
    assert response.json() == []


def test_create_post(client: TestClient, register_soul):
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret")
    post_payload = {
        "author_id": "1",
        "author_name": "Alice",
        "title": "Hello World",
        "content": "This is a social post.",
    }
    response = client.post(
        "/social/post",
        json=post_payload,
        headers={"X-Hub-Secret": "alice_secret"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "message_id" in data

    # Verify post in feed
    response = client.get("/social", headers={"X-Hub-Secret": "alice_secret"})
    posts = response.json()
    assert len(posts) == 1
    assert posts[0]["author_name"] == "Alice"
    assert posts[0]["title"] == "Hello World"


def test_reply_to_post(client: TestClient, register_soul):
    # Create post
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret")
    register_soul("2", essence=100.0, name="Bob", secret="bob_secret")

    post_payload = {
        "author_id": "1",
        "author_name": "Alice",
        "content": "First post",
    }
    res_post = client.post(
        "/social/post",
        json=post_payload,
        headers={"X-Hub-Secret": "alice_secret"},
    )
    message_id = res_post.json()["message_id"]

    # Reply
    reply_payload = {
        "message_id": message_id,
        "author_id": "2",
        "author_name": "Bob",
        "content": "Nice post!",
    }
    response = client.post(
        "/social/reply",
        json=reply_payload,
        headers={"X-Hub-Secret": "bob_secret"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    # Verify reply in feed
    response = client.get("/social", headers={"X-Hub-Secret": "alice_secret"})
    posts = response.json()
    assert len(posts[0]["replies"]) == 1
    assert posts[0]["replies"][0]["author_name"] == "Bob"
    assert posts[0]["replies"][0]["content"] == "Nice post!"


def test_reply_to_nonexistent_post(client: TestClient, register_soul):
    register_soul("2", essence=100.0, secret="bob_secret")
    reply_payload = {
        "message_id": "none",
        "author_id": "2",
        "author_name": "Bob",
        "content": "Ghost reply",
    }
    response = client.post(
        "/social/reply",
        json=reply_payload,
        headers={"X-Hub-Secret": "bob_secret"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Target message none not found"


def test_edit_message(client: TestClient, register_soul):
    # Create post
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret")
    post_res = client.post(
        "/social/post",
        json={
            "author_id": "1",
            "author_name": "Alice",
            "content": "Original content",
        },
        headers={"X-Hub-Secret": "alice_secret"},
    )
    message_id = post_res.json()["message_id"]

    # Edit post
    edit_res = client.post(
        f"/social/edit/{message_id}",
        json={"author_id": "1", "content": "Edited content"},
        headers={"X-Hub-Secret": "alice_secret"},
    )
    assert edit_res.status_code == 200

    # Verify edit
    get_res = client.get("/social", headers={"X-Hub-Secret": "alice_secret"})
    posts = get_res.json()
    assert posts[0]["content"] == "Edited content"

    # Unauthorized edit attempt
    register_soul("2", secret="bob_secret")
    fail_res = client.post(
        f"/social/edit/{message_id}",
        json={"author_id": "2", "content": "Hacker edit"},
        headers={"X-Hub-Secret": "bob_secret"},
    )
    assert fail_res.status_code == 404
    assert fail_res.json()["detail"] == "Message not found or unauthorized"


def test_delete_message(client: TestClient, register_soul):
    # Create post
    register_soul("1", essence=100.0, name="Alice", secret="alice_secret")
    post_res = client.post(
        "/social/post",
        json={
            "author_id": "1",
            "author_name": "Alice",
            "content": "To be deleted",
        },
        headers={"X-Hub-Secret": "alice_secret"},
    )
    message_id = post_res.json()["message_id"]

    # Unauthorized delete
    register_soul("2", secret="bob_secret")
    fail_res = client.post(
        f"/social/delete/{message_id}",
        json={"author_id": "2"},
        headers={"X-Hub-Secret": "bob_secret"},
    )
    assert fail_res.status_code == 404

    get_res = client.get("/social", headers={"X-Hub-Secret": "alice_secret"})
    assert len(get_res.json()) == 1

    # Authorized delete
    del_res = client.post(
        f"/social/delete/{message_id}",
        json={"author_id": "1"},
        headers={"X-Hub-Secret": "alice_secret"},
    )
    assert del_res.status_code == 200

    get_res = client.get("/social", headers={"X-Hub-Secret": "alice_secret"})
    assert len(get_res.json()) == 0


def test_operator_delete(client: TestClient, register_soul):
    # Create post
    register_soul("1", essence=100.0, name="Alice")
    post_res = client.post(
        "/social/post",
        json={
            "author_id": "1",
            "author_name": "Alice",
            "content": "To be deleted by Admin",
        },
    )
    message_id = post_res.json()["message_id"]

    # Operator delete (id 999)
    del_res = client.post(
        f"/social/delete/{message_id}",
        json={"author_id": "999"},
    )
    assert del_res.status_code == 200

    get_res = client.get("/social")
    assert len(get_res.json()) == 0
