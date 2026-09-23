import json
import time

import pytest
from fastapi.testclient import TestClient

from shared import protocol

from .. import database
from .. import viewport as vp


def _insert_soul(db_conn, soul_id, x=100.0, y=200.0, essence=50.0):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, essence) VALUES (?, ?, ?, ?)",
        (soul_id, f"owner_{soul_id}", json.dumps([x, y]), essence),
    )
    db_conn.commit()


class _Recorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)


def _make_stream(owner_id="owner1", ring_size=8, tick_id=0):
    manager = vp.ViewportManager()
    session = manager.create(owner_id, ring_size=ring_size)
    frame = vp.build_snapshot(session, protocol.SnapReason.FULL_SYNC, tick_id)
    return manager, session, frame


async def _flush_moves(session, positions_list, tick_id=0):
    rec = _Recorder()
    for positions in positions_list:
        for op, domain in vp.diff_positions(positions, session.committed):
            session.enqueue(op, domain)
        await vp.flush(session, positions, tick_id, rec)
    return rec


def test_diff_emits_move_upsert():
    ops = vp.diff_positions({"s1": (110.0, 200.0)}, {"s1": (100.0, 200.0)})
    assert len(ops) == 1
    op, domain = ops[0]
    assert domain == "move"
    assert op["op"] == protocol.EntityOpKind.UPSERT.value
    assert op["soul_id"] == "s1"
    assert "snap" not in op
    assert op["state"]["x"] == 110.0


def test_diff_ignores_sub_epsilon_jitter():
    ops = vp.diff_positions({"s1": (100.2, 200.0)}, {"s1": (100.0, 200.0)})
    assert ops == []


def test_diff_emits_snap_on_teleport():
    ops = vp.diff_positions({"s1": (900.0, 800.0)}, {"s1": (100.0, 200.0)})
    assert len(ops) == 1
    op, domain = ops[0]
    assert op["op"] == protocol.EntityOpKind.UPSERT.value
    assert op["snap"] is True


def test_diff_emits_snap_on_spawn():
    ops = vp.diff_positions({"s1": (100.0, 200.0)}, {})
    assert len(ops) == 1
    op, _ = ops[0]
    assert op["op"] == protocol.EntityOpKind.UPSERT.value
    assert op["snap"] is True


def test_diff_emits_remove():
    ops = vp.diff_positions({}, {"s1": (100.0, 200.0)})
    assert len(ops) == 1
    op, domain = ops[0]
    assert domain == "priority"
    assert op["op"] == protocol.EntityOpKind.REMOVE.value
    assert op["soul_id"] == "s1"
    assert op["state"] is None


@pytest.mark.anyio
async def test_enqueue_coalesces_moves_per_soul():
    _, session, _ = _make_stream()
    for i in range(10):
        op = vp._move_op("s1", 100.0 + i, 200.0)
        session.enqueue(op, "move")
    assert session.pending_count() == 1
    rec = _Recorder()
    result = await vp.flush(session, {"s1": (109.0, 200.0)}, 1, rec)
    assert result == "ok"
    assert len(rec.frames) == 1
    frame = rec.frames[0]
    assert frame["type"] == protocol.MessageType.DELTA.value
    assert frame["seq"] == 1
    assert frame["base_seq"] == 0
    assert len(frame["ops"]) == 1
    assert frame["ops"][0]["state"]["x"] == 109.0
    assert session.pending_count() == 0


@pytest.mark.anyio
async def test_enqueue_preserves_remove_and_economy():
    _, session, _ = _make_stream()
    for i in range(5):
        session.enqueue(vp._move_op("s1", 100.0 + i, 200.0), "move")
    session.enqueue(vp._remove_op("s2"), "priority")
    session.enqueue(vp._economy_op("s1", 77.5), "priority")
    rec = _Recorder()
    result = await vp.flush(session, {"s1": (104.0, 200.0)}, 1, rec)
    assert result == "ok"
    ops = rec.frames[0]["ops"]
    assert len(ops) == 3
    kinds = [(o["op"], o.get("domain")) for o in ops]
    assert ("remove", None) in kinds
    assert ("upsert", "economy") in kinds
    moves = [o for o in ops if o.get("domain") != "economy"]
    assert len(moves) == 2
    econ = [o for o in ops if o.get("domain") == "economy"][0]
    assert econ["state"]["essence"] == 77.5


@pytest.mark.anyio
async def test_snap_flag_survives_coalescing():
    _, session, _ = _make_stream()
    session.enqueue(vp._move_op("s1", 900.0, 800.0, snap=True), "move")
    session.enqueue(vp._move_op("s1", 901.0, 800.0), "move")
    rec = _Recorder()
    await vp.flush(session, {"s1": (901.0, 800.0)}, 1, rec)
    ops = rec.frames[0]["ops"]
    assert len(ops) == 1
    assert ops[0]["snap"] is True


@pytest.mark.anyio
async def test_backpressure_downgrades_then_closes(monkeypatch):
    monkeypatch.setattr(vp, "HIGH_WATER_OPS", 4)
    monkeypatch.setattr(vp, "MAX_SLOW_FLUSHES", 3)
    _, session, _ = _make_stream()
    for i in range(5):
        session.enqueue(vp._move_op(f"s{i}", 100.0, 200.0), "move")
    rec = _Recorder()
    assert await vp.flush(session, {}, 1, rec) == "saturated"
    assert session.flush_interval == pytest.approx(vp.PUMP_INTERVAL_SECONDS * 2)
    assert session.pending_count() == 5
    assert await vp.flush(session, {}, 1, rec) == "saturated"
    assert session.flush_interval == pytest.approx(vp.PUMP_INTERVAL_SECONDS * 4)
    assert await vp.flush(session, {}, 1, rec) == "closed"
    assert rec.frames == []


@pytest.mark.anyio
async def test_backpressure_recovers_on_drain(monkeypatch):
    monkeypatch.setattr(vp, "HIGH_WATER_OPS", 4)
    _, session, _ = _make_stream()
    for i in range(5):
        session.enqueue(vp._move_op(f"s{i}", 100.0, 200.0), "move")
    rec = _Recorder()
    assert await vp.flush(session, {}, 1, rec) == "saturated"
    session.pending_moves.clear()
    session.pending_priority.clear()
    assert await vp.flush(session, {}, 1, rec) == "ok"
    assert session.flush_interval == pytest.approx(vp.PUMP_INTERVAL_SECONDS)
    assert session.slow_flushes == 0


@pytest.mark.anyio
async def test_resume_inside_window_replays_no_resnapshot():
    manager, old, _ = _make_stream(ring_size=4)
    rec = await _flush_moves(
        old,
        [{"s1": (110.0, 200.0)}, {"s1": (120.0, 200.0)}, {"s1": (130.0, 200.0)}],
    )
    assert [f["seq"] for f in old.ring] == [1, 2, 3]
    new = manager.create("owner1")
    replay = _Recorder()
    outcome = await manager.resume(new, old.conn_id, 1, 9, replay)
    assert outcome == "replayed"
    assert len(replay.frames) == 2
    assert replay.frames[0]["type"] == protocol.MessageType.DELTA.value
    assert replay.frames[0]["ops"] == rec.frames[1]["ops"]
    assert replay.frames[1]["ops"] == rec.frames[2]["ops"]
    assert replay.frames[0]["seq"] == 0
    assert replay.frames[1]["seq"] == 1
    assert new.next_seq == 2
    assert new.committed == old.committed
    assert manager.get(old.conn_id) is None


@pytest.mark.anyio
async def test_resume_fully_caught_up_sends_nothing():
    manager, old, _ = _make_stream(ring_size=4)
    await _flush_moves(old, [{"s1": (110.0, 200.0)}])
    new = manager.create("owner1")
    replay = _Recorder()
    outcome = await manager.resume(new, old.conn_id, 1, 9, replay)
    assert outcome == "replayed"
    assert replay.frames == []


@pytest.mark.anyio
async def test_resume_outside_window_returns_snapshot():
    manager, old, _ = _make_stream(ring_size=2)
    await _flush_moves(
        old,
        [
            {"s1": (110.0, 200.0)},
            {"s1": (120.0, 200.0)},
            {"s1": (130.0, 200.0)},
            {"s1": (140.0, 200.0)},
        ],
    )
    assert [f["seq"] for f in old.ring] == [3, 4]
    new = manager.create("owner1")
    replay = _Recorder()
    outcome = await manager.resume(new, old.conn_id, 1, 9, replay)
    assert outcome == "snapshot"
    assert replay.frames == []


@pytest.mark.anyio
async def test_resume_unknown_conn_id_returns_snapshot():
    manager, _, _ = _make_stream()
    new = manager.create("owner1")
    replay = _Recorder()
    outcome = await manager.resume(new, "nope", 0, 9, replay)
    assert outcome == "snapshot"


@pytest.mark.anyio
async def test_resume_wrong_owner_returns_snapshot():
    manager, old, _ = _make_stream(owner_id="owner1")
    other = manager.create("owner2")
    replay = _Recorder()
    outcome = await manager.resume(other, old.conn_id, 0, 9, replay)
    assert outcome == "snapshot"


@pytest.mark.anyio
async def test_resume_expired_session_returns_snapshot(monkeypatch):
    monkeypatch.setattr(vp, "RESUME_TTL_SECONDS", 0)
    manager, old, _ = _make_stream()
    new = manager.create("owner1")
    replay = _Recorder()
    outcome = await manager.resume(new, old.conn_id, 0, 9, replay)
    assert outcome == "snapshot"


def test_notify_economy_enqueues_priority_op(db_conn):
    _insert_soul(db_conn, "s1", essence=50.0)
    manager = vp.ViewportManager()
    session = manager.create("owner_s1")
    count = manager.notify_economy_soul("s1")
    assert count == 1
    assert len(session.pending_priority) == 1
    op = session.pending_priority[0]
    assert op["domain"] == "economy"
    assert op["state"]["essence"] == 50.0
    assert manager.notify_economy_soul("ghost") == 0


def test_build_snapshot_conforms_to_protocol(db_conn):
    _insert_soul(db_conn, "s1", x=100.0, y=200.0, essence=42.0)
    manager = vp.ViewportManager()
    session = manager.create("owner_s1")
    frame = vp.build_snapshot(session, protocol.SnapReason.JOIN, 7)
    parsed = protocol.parse_envelope(frame)
    assert parsed.type == protocol.MessageType.SNAPSHOT.value
    assert parsed.v == protocol.PROTOCOL_VERSION
    snapshot = protocol.Snapshot(
        reason=frame["reason"], tick_id=frame["tick_id"], souls=frame["souls"]
    )
    assert snapshot.reason == protocol.SnapReason.JOIN
    assert snapshot.tick_id == 7
    assert len(snapshot.souls) == 1
    assert snapshot.souls[0].soul_id == "s1"
    assert frame["seq"] == 0
    assert frame["conn_id"] == session.conn_id
    assert frame["region"] == {"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0}
    assert frame["plots"] == []
    assert frame["wallets"] == [{"soul_id": "s1", "essence": 42.0}]
    assert session.next_seq == 1
    assert session.committed == {"s1": (100.0, 200.0)}


def test_websocket_snapshot_on_connect(client: TestClient, db_conn):
    _insert_soul(db_conn, "s1", x=100.0, y=200.0, essence=42.0)
    with client.websocket_connect("/ws/user1") as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        assert connected["v"] == 1
        assert connected["conn_id"]
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["v"] == 1
        assert snapshot["conn_id"] == connected["conn_id"]
        assert snapshot["reason"] == "join"
        assert isinstance(snapshot["seq"], int)
        assert snapshot["region"]["w"] == 1920.0
        assert snapshot["plots"] == []
        assert {"soul_id": "s1", "essence": 42.0} in snapshot["wallets"]
        souls = {s["soul_id"]: s for s in snapshot["souls"]}
        assert souls["s1"]["x"] == 100.0


def test_websocket_resume_inside_window_replays_delta(client: TestClient, db_conn):
    _insert_soul(db_conn, "s1", x=100.0, y=200.0)
    with client.websocket_connect("/ws/user1") as ws1:
        connected = ws1.receive_json()
        old_conn_id = connected["conn_id"]
        ws1.receive_json()
        time.sleep(0.5)
        db_conn.execute(
            "UPDATE souls SET position = ? WHERE soul_id = ?",
            (json.dumps([150.0, 200.0]), "s1"),
        )
        db_conn.commit()
        time.sleep(0.8)

    with client.websocket_connect("/ws/user1") as ws2:
        ws2.receive_json()
        ws2.receive_json()
        ws2.send_json({"type": "resume", "conn_id": old_conn_id, "last_seq": 0})
        delta = ws2.receive_json()
        assert delta["type"] == "delta"
        assert delta["v"] == 1
        ops = [o for o in delta["ops"] if o["soul_id"] == "s1"]
        assert len(ops) == 1
        assert ops[0]["op"] == "upsert"
        assert ops[0]["state"]["x"] == 150.0


def test_websocket_resume_expired_gets_fresh_snapshot(
    client: TestClient, db_conn, monkeypatch
):
    monkeypatch.setattr(vp, "RESUME_TTL_SECONDS", 0)
    with client.websocket_connect("/ws/user1") as ws1:
        old_conn_id = ws1.receive_json()["conn_id"]
        ws1.receive_json()

    with client.websocket_connect("/ws/user1") as ws2:
        ws2.receive_json()
        ws2.receive_json()
        ws2.send_json({"type": "resume", "conn_id": old_conn_id, "last_seq": 0})
        snapshot = ws2.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["reason"] == "resync"


def test_economy_op_flows_through_marketplace_buy(
    client: TestClient, db_conn, register_soul
):
    register_soul("buyer1", essence=1000.0)
    register_soul("seller1", essence=100.0)
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO soul_inventory (soul_id, item_name, quantity) "
            "VALUES (?, ?, ?)",
            ("seller1", "orb", 1),
        )
        conn.commit()
    listing = client.post(
        "/marketplace/list",
        json={
            "seller_id": "seller1",
            "seller_name": "Seller One",
            "item": {"name": "orb"},
            "price": 200.0,
        },
    )
    assert listing.status_code == 200
    listing_id = listing.json()["listing_id"]
    session = vp.viewport.create("owner_buyer1")
    bought = client.post(f"/marketplace/buy/{listing_id}", json={"buyer_id": "buyer1"})
    assert bought.status_code == 200
    row = db_conn.execute(
        "SELECT essence FROM souls WHERE soul_id = ?", ("buyer1",)
    ).fetchone()
    assert len(session.pending_priority) == 1
    op = session.pending_priority[0]
    assert op["domain"] == "economy"
    assert op["soul_id"] == "buyer1"
    assert op["state"]["essence"] == row["essence"]
    vp.viewport.drop(session.conn_id)


@pytest.mark.anyio
async def test_bubble_op_sent_but_never_replayed():
    manager, session, _ = _make_stream(ring_size=4)
    assert manager.notify_bubble("owner1", "s1", "a quip!", kind="quip",
                                 solicited=True) == 1
    rec = _Recorder()
    assert await vp.flush(session, {}, 1, rec) == "ok"
    # The bubble goes out on the wire...
    sent_ops = rec.frames[0]["ops"]
    assert [op["op"] for op in sent_ops] == ["bubble"]
    assert sent_ops[0]["kind"] == "quip"
    assert sent_ops[0]["solicited"] is True
    # ...but the ring copy is stripped so resume never replays it.
    assert session.ring[-1]["ops"] == []
    new = manager.create("owner1")
    replay = _Recorder()
    outcome = await manager.resume(new, session.conn_id, 0, 9, replay)
    assert outcome == "replayed"
    assert replay.frames[0]["ops"] == []
