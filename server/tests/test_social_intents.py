"""Issue #18 evidence: intents, escrow atomicity, depth, tombstones,
actor typing, idempotency, and the legacy expand-contract migration.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from .. import database
from .. import social as social_lib
from ..main import app


@pytest.fixture
def tamer_a():
    c = TestClient(app)
    r = c.post(
        "/tamers/register",
        json={"username": "tamer_a", "password": "s3cur3pass"},
    )
    assert r.status_code == 201
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login",
        json={"username": "tamer_a", "password": "s3cur3pass"},
    )
    assert r.status_code == 200
    return {"tamer_id": tamer_id, "token": r.json()["token"]}


def _essence(soul_id: str) -> float:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
        return float(row["essence"])


def _op_post(
    client: TestClient, soul_id: str, title="T", content="b", author_type="soul"
):
    """Create a post as the operator naming a soul/tamer author."""
    res = client.post(
        "/social/post",
        json={
            "author_id": soul_id,
            "author_type": author_type,
            "author_name": "Op",
            "title": title,
            "content": content,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["message_id"]


def _op_reply(
    client: TestClient, parent_id: str, soul_id: str, content="r", author_type="soul"
):
    res = client.post(
        "/social/reply",
        json={
            "message_id": parent_id,
            "author_id": soul_id,
            "author_type": author_type,
            "author_name": "Op",
            "content": content,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["reply_id"]


def _walk(root: dict):
    """Yield (node, depth) down the single-child spine of a tree."""
    node, depth = root, 0
    while True:
        yield node, depth
        if not node["replies"]:
            return
        node, depth = node["replies"][0], depth + 1


def test_deep_thread_nesting_edit_and_tombstone(client: TestClient, register_soul):
    register_soul("deep", essence=1000.0, secret="deep_secret_key_12345")
    root_id = _op_post(client, "deep", title="Root", content="root")
    parent = root_id
    ids = []
    for i in range(12):
        parent = _op_reply(client, parent, "deep", content=f"depth {i + 1}")
        ids.append(parent)

    trees = client.get("/social").json()
    assert len(trees) == 1
    spine = list(_walk(trees[0]))
    assert [d for _n, d in spine] == list(range(13))
    assert spine[12][0]["body"] == "depth 12"

    # Edit a deep node as the soul identity; free, timestamped.
    deep_node = spine[10][0]
    res = client.post(
        f"/social/edit/{deep_node['message_id']}",
        json={"author_id": "deep", "content": "edited at ten"},
        headers={"X-Hub-Secret": "deep_secret_key_12345"},
    )
    assert res.status_code == 200

    # Soft-delete the ancestor at depth 5; children stay reachable.
    doomed = spine[5][0]
    res = client.post(
        f"/social/delete/{doomed['message_id']}",
        json={"author_id": "deep"},
        headers={"X-Hub-Secret": "deep_secret_key_12345"},
    )
    assert res.status_code == 200

    trees = client.get("/social").json()
    spine = list(_walk(trees[0]))
    assert len(spine) == 13
    assert spine[5][0]["deleted"] is True
    assert spine[5][0]["body"] == "[deleted]"
    assert spine[6][0]["deleted"] is False
    assert spine[6][0]["body"] == "depth 6"
    assert spine[10][0]["body"] == "edited at ten"
    with database.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        assert n == 13


def test_depth_cap_refused_and_refunded(client: TestClient, register_soul):
    register_soul("cap", essence=10000.0)
    before = _essence("cap")
    root_id = _op_post(client, "cap", title="Cap", content="cap")
    parent = root_id
    for i in range(social_lib.MAX_DEPTH):
        parent = _op_reply(client, parent, "cap", content=f"d{i}")
    # One more reply would sit at depth 26: refused, hold released.
    res = client.post(
        "/social/reply",
        json={
            "message_id": parent,
            "author_id": "cap",
            "author_name": "Op",
            "content": "too deep",
        },
    )
    assert res.status_code == 400
    spent = 20.0 + 8.0 * social_lib.MAX_DEPTH
    assert _essence("cap") == before - spent


def test_post_charge_is_atomic(client: TestClient, register_soul):
    register_soul("atom", essence=100.0)
    res = client.post(
        "/social/post",
        json={
            "author_id": "atom",
            "author_name": "A",
            "title": "Atomic",
            "content": "body",
        },
    )
    assert res.status_code == 200
    message_id = res.json()["message_id"]

    with database.get_db() as conn:
        msg = conn.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        assert msg is not None
        intent = conn.execute(
            "SELECT * FROM intents WHERE kind = 'social_post'"
        ).fetchone()
        assert intent["status"] == "adjudicated"
        escrow = conn.execute(
            "SELECT * FROM escrows WHERE intent_id = ?",
            (intent["intent_id"],),
        ).fetchone()
        assert escrow["status"] == "applied"
        assert float(escrow["amount"]) == 20.0
        ledgers = conn.execute(
            "SELECT * FROM ledger WHERE intent_id = ?",
            (intent["intent_id"],),
        ).fetchall()
        assert len(ledgers) == 1
        assert ledgers[0]["entry_type"] == "debit"
        assert float(ledgers[0]["amount"]) == 20.0
        assert ledgers[0]["soul_id"] == "atom"
    assert _essence("atom") == 80.0


def test_insufficient_funds_leaves_no_trace(client: TestClient, register_soul):
    register_soul("poor", essence=10.0)
    res = client.post(
        "/social/post",
        json={
            "author_id": "poor",
            "author_name": "P",
            "title": "Broke",
            "content": "nope",
        },
    )
    assert res.status_code == 400
    assert "Insufficient" in res.json()["detail"]
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM ledger WHERE entry_type != 'mint'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM escrows").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM intents").fetchone()["n"] == 0
    assert _essence("poor") == 10.0


def test_reply_to_deleted_parent_refused_at_preface(client: TestClient, register_soul):
    register_soul("ref", essence=100.0)
    root = _op_post(client, "ref", title="Doomed", content="x")
    client.post(f"/social/delete/{root}", json={"author_id": "ref"})
    res = client.post(
        "/social/reply",
        json={
            "message_id": root,
            "author_id": "ref",
            "author_name": "R",
            "content": "ghost",
        },
    )
    assert res.status_code == 400
    assert "deleted" in res.json()["detail"].lower()
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE message_id != ?", (root,)
        ).fetchall()
        assert rows == []
        # Only the post's own debit exists; the refused reply wrote none.
        assert conn.execute("SELECT COUNT(*) AS n FROM ledger WHERE entry_type != 'mint'").fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM escrows WHERE status = 'held'"
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM intents WHERE kind = 'social_reply'"
            ).fetchone()["n"]
            == 0
        )
    assert _essence("ref") == 80.0


def test_reply_refused_at_adjudication_refunds(client: TestClient, register_soul):
    """Parent deleted between enqueue and adjudication: the held escrow
    is released, no message or ledger rows are written."""
    from ..world_tick import WorldTick

    register_soul("adj", essence=100.0)
    root = _op_post(client, "adj", title="Doomed", content="x")
    record, created = social_lib.enqueue_social_intent(
        "test-session",
        "adj-nonce-1",
        "owner_adj",
        "adj",
        social_lib.KIND_SOCIAL_REPLY,
        {
            "parent_id": root,
            "body": "ghost",
            "author_type": "soul",
            "author_id": "adj",
            "author_name": "A",
        },
    )
    assert created
    assert record["status"] == "pending"
    assert _essence("adj") == 72.0  # 20 post + 8 held reply

    # The parent is deleted between enqueue and adjudication.
    with database.get_db() as conn:
        conn.execute("UPDATE messages SET deleted = 1 WHERE message_id = ?", (root,))
        conn.commit()
    WorldTick().pump_intents()

    with database.get_db() as conn:
        intent = conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
        assert intent["status"] == "rejected"
        assert json.loads(intent["result"])["reason"] == "parent_deleted"
        escrow = conn.execute(
            "SELECT * FROM escrows WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
        assert escrow["status"] == "released"
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE message_id != ?",
                (root,),
            ).fetchone()["n"]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) AS n FROM ledger WHERE entry_type != 'mint'").fetchone()["n"] == 1
    assert _essence("adj") == 80.0


def test_mixed_market_social_conservation(client: TestClient, register_soul):
    register_soul("m1", essence=200.0)
    register_soul("m2", essence=200.0)
    client.post(
        "/social/post",
        json={"author_id": "m1", "author_name": "M1", "title": "A", "content": "a"},
    )
    client.post(
        "/social/post",
        json={"author_id": "m2", "author_name": "M2", "title": "B", "content": "b"},
    )
    from ..market import verify_balances

    with database.get_db() as conn:
        result = verify_balances(conn, repair=False)
    assert result["ok"] is True, result["issues"]
    assert _essence("m1") == 180.0
    assert _essence("m2") == 180.0


def test_idempotency_key_prevents_double_charge(client: TestClient, register_soul):
    register_soul("idem", essence=100.0)
    headers = {"Idempotency-Key": "social-key-1"}
    payload = {
        "author_id": "idem",
        "author_name": "I",
        "title": "Once",
        "content": "body",
    }
    first = client.post("/social/post", json=payload, headers=headers)
    assert first.status_code == 200
    second = client.post("/social/post", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["message_id"] == first.json()["message_id"]
    assert _essence("idem") == 80.0
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM intents WHERE kind = 'social_post'"
            ).fetchone()["n"]
            == 1
        )


def test_tamer_authors_free(client: TestClient, tamer_a):
    token = tamer_a["token"]
    tc = TestClient(
        client.app,
        headers={"X-Hub-Secret": token},
    )
    res = tc.post(
        "/social/post",
        json={"title": "Tamer voice", "content": "hello from tamer"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["cost"] == 0.0
    message_id = res.json()["message_id"]
    with database.get_db() as conn:
        msg = conn.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        assert msg["author_type"] == "tamer"
        assert msg["author_id"] == tamer_a["tamer_id"]
        assert conn.execute("SELECT COUNT(*) AS n FROM ledger WHERE entry_type != 'mint'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM escrows").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM intents WHERE kind = 'social_post'"
            ).fetchone()["n"]
            == 1
        )


def test_tamer_cannot_author_as_unowned_soul(
    client: TestClient, tamer_a, register_soul
):
    register_soul("stranger", essence=100.0)
    tc = TestClient(client.app, headers={"X-Hub-Secret": tamer_a["token"]})
    res = tc.post(
        "/social/post",
        json={
            "author_type": "soul",
            "author_id": "stranger",
            "title": "Hijack",
            "content": "x",
        },
    )
    assert res.status_code == 403
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0
    assert _essence("stranger") == 100.0


def test_tamer_authors_as_own_soul_and_soul_pays(client: TestClient, tamer_a):
    tc = TestClient(client.app, headers={"X-Hub-Secret": tamer_a["token"]})
    res = tc.post(
        "/souls",
        json={
            "custodian_id": tamer_a["tamer_id"],
            "souls": [{"soul_id": "tsoul", "name": "TamerSoul"}],
        },
    )
    assert res.status_code == 200
    with database.get_db() as conn:
        conn.execute("UPDATE souls SET essence = 100.0 WHERE soul_id = 'tsoul'")
        conn.commit()
    res = tc.post(
        "/social/post",
        json={
            "author_type": "soul",
            "author_id": "tsoul",
            "author_name": "TamerSoul",
            "title": "Custody",
            "content": "charged",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["cost"] == 20.0
    assert _essence("tsoul") == 80.0
    with database.get_db() as conn:
        msg = conn.execute("SELECT * FROM messages WHERE title = 'Custody'").fetchone()
        assert msg["author_type"] == "soul"
        assert msg["author_id"] == "tsoul"


def test_operator_edit_any_message_audit_logged(client: TestClient, register_soul):
    register_soul("editme", essence=100.0)
    message_id = _op_post(client, "editme", title="Op edit", content="before")
    res = client.post(
        f"/social/edit/{message_id}",
        json={"author_id": "999", "content": "after"},
    )
    assert res.status_code == 200
    trees = client.get("/social").json()
    assert trees[0]["body"] == "after"
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT action FROM audit_log WHERE target_id = ? AND action = "
            "'social_edit_as_operator'",
            (message_id,),
        ).fetchone()
        assert row is not None


def test_edit_deleted_message_rejected(client: TestClient, register_soul):
    register_soul("gone", essence=100.0)
    message_id = _op_post(client, "gone", title="Gone", content="x")
    client.post(f"/social/delete/{message_id}", json={"author_id": "gone"})
    res = client.post(
        f"/social/edit/{message_id}",
        json={"author_id": "gone", "content": "zombie"},
    )
    assert res.status_code == 404
    trees = client.get("/social").json()
    assert trees[0]["body"] == "[deleted]"


def test_delete_is_idempotent(client: TestClient, register_soul):
    register_soul("twice", essence=100.0)
    message_id = _op_post(client, "twice", title="Twice", content="x")
    for _ in range(2):
        res = client.post(f"/social/delete/{message_id}", json={"author_id": "twice"})
        assert res.status_code == 200
    trees = client.get("/social").json()
    assert len(trees) == 1
    assert trees[0]["deleted"] is True


def test_operator_cannot_author_as_missing_tamer(client: TestClient):
    res = client.post(
        "/social/post",
        json={
            "author_type": "tamer",
            "author_id": "ghost_tamer",
            "author_name": "Ghost",
            "title": "Nope",
            "content": "x",
        },
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_adjudication_refuses_without_held_escrow(client: TestClient,
                                                 register_soul):
    """Malformed state (hold vanished after enqueue) must not create a
    message or ledger debit."""
    from ..world_tick import WorldTick

    register_soul("esc", essence=100.0)
    record, created = social_lib.enqueue_social_intent(
        "test-session",
        "esc-nonce-1",
        "owner_esc",
        "esc",
        social_lib.KIND_SOCIAL_POST,
        {
            "title": "Held?",
            "body": "x",
            "author_type": "soul",
            "author_id": "esc",
            "author_name": "E",
        },
    )
    assert created
    with database.get_db() as conn:
        conn.execute(
            "DELETE FROM escrows WHERE intent_id = ?", (record["intent_id"],)
        )
        conn.commit()
    WorldTick().pump_intents()
    with database.get_db() as conn:
        intent = conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (record["intent_id"],)
        ).fetchone()
        assert intent["status"] == "rejected"
        assert json.loads(intent["result"])["reason"] == "escrow_missing"
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM ledger WHERE entry_type != 'mint'"
            ).fetchone()["n"]
            == 0
        )
    # The hold deduction is NOT refunded here: the hold row itself is
    # gone, so there is nothing to release against. The invariant that
    # matters -- no message or debit without a hold -- holds.
    assert _essence("esc") == 80.0


def test_legacy_migration_expand_contract(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE social_posts (message_id TEXT PRIMARY KEY, "
        "author_id TEXT, author_name TEXT, title TEXT, content TEXT, "
        "timestamp REAL)"
    )
    raw.execute(
        "CREATE TABLE social_replies (reply_id TEXT PRIMARY KEY, "
        "parent_id TEXT, author_id TEXT, author_name TEXT, content TEXT, "
        "timestamp REAL)"
    )
    raw.executemany(
        "INSERT INTO social_posts VALUES (?,?,?,?,?,?)",
        [
            ("p1", "s1", "Alice", "Hello", "<b>Hi</b> [there] {x}", 1000.0),
            ("p2", "s2", "Bob", "", "untitled legacy", 1001.0),
            ("p3", "s3", "Zed", "Doomed", "vanishes", 1002.0),
        ],
    )
    raw.executemany(
        "INSERT INTO social_replies VALUES (?,?,?,?,?,?)",
        [
            ("r1", "p1", "s2", "Bob", "Nice <i>post</i>!", 1010.0),
            ("r2", "r1", "s1", "Alice", "thanks [buddy]", 1011.0),
        ],
    )
    # Reply to a post that is then deleted: dangling parent preserved.
    raw.execute(
        "INSERT INTO social_replies VALUES ('r3','p3','s9','Zed','orphan',1012.0)"
    )
    raw.commit()
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM social_posts WHERE message_id = 'p3'")
    raw.execute("PRAGMA foreign_keys=ON")
    raw.commit()
    raw.close()

    old_path = database.DB_PATH
    database.DB_PATH = db_path
    try:
        database.init_db()
    finally:
        database.DB_PATH = old_path

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "social_posts" not in tables
    assert "social_replies" not in tables
    rows = {r["message_id"]: r for r in conn.execute("SELECT * FROM messages")}
    assert set(rows) == {"p1", "p2", "r1", "r2", "r3"}

    p1 = rows["p1"]
    assert p1["parent_id"] is None
    assert p1["author_type"] == "soul"
    assert p1["author_id"] == "s1"
    assert p1["title"] == "Hello"
    assert p1["body"] == "<b>Hi</b> [there] {x}"
    assert p1["created_at"] == 1000.0
    assert p1["deleted"] == 0

    # Legacy empty titles become the deterministic placeholder.
    assert rows["p2"]["title"] == "(untitled)"
    assert rows["p2"]["body"] == "untitled legacy"

    assert rows["r1"]["parent_id"] == "p1"
    assert rows["r1"]["title"] is None
    assert rows["r1"]["body"] == "Nice <i>post</i>!"
    assert rows["r2"]["parent_id"] == "r1"
    # Dangling parent of the deleted post is preserved, not discarded.
    assert rows["r3"]["parent_id"] == "p3"

    tree = social_lib.build_tree(conn)
    roots = {n["message_id"] for n in tree}
    assert {"p1", "p2"} <= roots
    p1_node = next(n for n in tree if n["message_id"] == "p1")
    r1_node = p1_node["replies"][0]
    assert r1_node["message_id"] == "r1"
    assert r1_node["replies"][0]["message_id"] == "r2"
    conn.close()

    # The migrated table still accepts writes under the new constraints.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO messages (message_id, parent_id, author_type, author_id,"
        " author_name, title, body, created_at) VALUES "
        "('new1', NULL, 'soul', 's1', 'A', 'Fresh', 'works', 2000.0)"
    )
    conn.execute(
        "INSERT INTO messages (message_id, parent_id, author_type, author_id,"
        " author_name, title, body, created_at) VALUES "
        "('new2', 'new1', 'tamer', 't1', 'T', NULL, 'reply', 2001.0)"
    )
    conn.commit()
    conn.close()


def test_orphan_reply_renders_as_root(tmp_path):
    db_path = str(tmp_path / "orphan.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE social_posts (message_id TEXT PRIMARY KEY, "
        "author_id TEXT, author_name TEXT, title TEXT, content TEXT, "
        "timestamp REAL)"
    )
    raw.execute(
        "CREATE TABLE social_replies (reply_id TEXT PRIMARY KEY, "
        "parent_id TEXT, author_id TEXT, author_name TEXT, content TEXT, "
        "timestamp REAL)"
    )
    raw.execute("INSERT INTO social_replies VALUES ('only','ghost','s1','A','x',1.0)")
    raw.commit()
    raw.close()
    old_path = database.DB_PATH
    database.DB_PATH = db_path
    try:
        database.init_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tree = social_lib.build_tree(conn)
        conn.close()
    finally:
        database.DB_PATH = old_path
    assert len(tree) == 1
    assert tree[0]["message_id"] == "only"
