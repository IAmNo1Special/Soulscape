from shared import protocol
from shared.protocol import (
    PROTOCOL_VERSION,
    Delta,
    Envelope,
    EntityOp,
    EntityOpKind,
    Intent,
    MessageType,
    SnapReason,
    Snapshot,
    SoulWireState,
)


def test_version_stamp():
    assert PROTOCOL_VERSION == 1
    env = protocol.envelope(MessageType.PING)
    assert env["v"] == 1
    assert env["type"] == "ping"


def test_envelope_roundtrip():
    raw = protocol.envelope(
        MessageType.SOUL_UPDATED, owner_id="o1", souls=[{"soul_id": "s1"}]
    )
    env = protocol.parse_envelope(raw)
    assert env.v == 1
    assert env.type == "soul_updated"
    assert isinstance(env, Envelope)


def test_legacy_message_detected():
    assert protocol.is_legacy({"type": "ping"})
    assert not protocol.is_legacy({"v": 1, "type": "ping"})


def test_snapshot_model():
    snap = Snapshot(
        reason=SnapReason.JOIN,
        tick_id=42,
        souls=[SoulWireState(soul_id="s1", x=1.0, y=2.0)],
    )
    assert snap.reason == SnapReason.JOIN
    assert snap.souls[0].soul_id == "s1"
    assert isinstance(snap.souls[0].soul_id, str)


def test_delta_entity_ops():
    delta = Delta(
        tick_id=7,
        ops=[
            EntityOp(
                op=EntityOpKind.UPSERT,
                soul_id="s1",
                state=SoulWireState(soul_id="s1", x=3.0),
            ),
            EntityOp(op=EntityOpKind.REMOVE, soul_id="s2"),
        ],
    )
    assert delta.ops[0].op == EntityOpKind.UPSERT
    assert delta.ops[1].state is None
    raw = delta.model_dump()
    assert raw["ops"][0]["state"]["x"] == 3.0


def test_intent_model():
    intent = Intent(
        intent="soul_update",
        session_id="sess",
        signature="sig",
        nonce="n",
        payload={"souls": []},
    )
    assert intent.intent == "soul_update"
    assert intent.signature == "sig"


def test_soul_wire_state_requires_str_id():
    state = SoulWireState(soul_id="abc")
    assert state.soul_id == "abc"
