"""IPC protocol unit tests (issue #37).

Covers the length-prefixed JSON framing, the SimDispatcher message
table, and the in-process + TCP backends -- without spawning real
processes (the two-process survival test lives in
test_sim_survives_api_restart.py).
"""

import logging
import socket
import time

import pytest

from ..sim_gateway import InProcessBackend, SimGateway
from ..sim_ipc import (
    FRAME_HEADER,
    MAX_FRAME_BYTES,
    SimClient,
    SimCommandError,
    SimDispatcher,
    SimError,
    SimHost,
    SimRefusal,
    SimServer,
    SimUnreachable,
    recv_frame,
    send_frame,
)


@pytest.fixture
def dispatcher():
    return SimDispatcher()


@pytest.fixture
def gateway():
    return SimGateway(InProcessBackend())


def test_frame_round_trip():
    a, b = socket.socketpair()
    try:
        send_frame(a, {"type": "ping", "n": 1})
        assert recv_frame(b, timeout=2.0) == {"type": "ping", "n": 1}
    finally:
        a.close()
        b.close()


def test_frame_rejects_oversize():
    a, b = socket.socketpair()
    try:
        # A header claiming more than MAX_FRAME_BYTES must be refused
        # before any payload is read.
        a.sendall(FRAME_HEADER.pack(MAX_FRAME_BYTES + 1))
        with pytest.raises(SimError):
            recv_frame(b, timeout=2.0)
    finally:
        a.close()
        b.close()


def test_frame_rejects_garbage_header():
    a, b = socket.socketpair()
    try:
        a.sendall(b"\xff\xff")  # truncated header
        a.shutdown(socket.SHUT_WR)
        with pytest.raises(SimUnreachable):
            recv_frame(b, timeout=2.0)
    finally:
        a.close()
        b.close()


def test_dispatcher_ping(dispatcher):
    resp = dispatcher.handle({"type": "ping"})
    assert resp["ok"] is True
    assert isinstance(resp["tick_running"], bool)
    assert isinstance(resp["tick_id"], int)


def test_dispatcher_unknown_message_type(dispatcher):
    resp = dispatcher.handle({"type": "nope"})
    assert resp["ok"] is False
    assert resp["error"]["reason"] == "unknown_message_type"


def test_dispatcher_unknown_command(dispatcher):
    resp = dispatcher.handle({"type": "command", "name": "nope", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["reason"] == "unknown_command"


def test_dispatcher_command_error_shape(dispatcher):
    # A command that raises SimCommandError serializes reason+detail.
    from .. import sim_commands

    @sim_commands.command("test_boom")
    def _boom(params, tick):
        raise SimCommandError("test_reason", "test detail")

    try:
        resp = dispatcher.handle({"type": "command", "name": "test_boom", "params": {}})
        assert resp["ok"] is False
        assert resp["error"]["reason"] == "test_reason"
        assert resp["error"]["detail"] == "test detail"
    finally:
        del sim_commands.COMMANDS["test_boom"]


def test_dispatcher_command_receives_tick(dispatcher):
    # Regression: the dispatcher must call fn(params, tick), not
    # fn(params).
    from .. import sim_commands

    seen = {}

    @sim_commands.command("test_tick_probe")
    def _probe(params, tick):
        seen["tick"] = tick
        return {"ok": True}

    try:
        resp = dispatcher.handle(
            {"type": "command", "name": "test_tick_probe", "params": {}}
        )
        assert resp["ok"] is True
        assert seen["tick"] is dispatcher.tick
    finally:
        del sim_commands.COMMANDS["test_tick_probe"]


def test_inprocess_submit_and_status(gateway):
    record = gateway.submit_intent(
        "sess",
        "nonce-ipc-1",
        "tamer1",
        "soul1",
        "move_to",
        {"x": 1.0, "y": 2.0},
    )
    assert record["nonce"] == "nonce-ipc-1"
    assert record["status"] == "pending"
    fresh = gateway.intent_status("sess", "nonce-ipc-1")
    assert fresh is not None
    assert fresh["intent_id"] == record["intent_id"]


def test_inprocess_submit_refusal_shape(gateway):
    # tamer_presence with a bad payload is refused with reason+detail.
    with pytest.raises(SimRefusal) as excinfo:
        gateway.submit_intent(
            "sess",
            "nonce-ipc-2",
            "tamer1",
            "tamer:tamer1",
            "tamer_presence",
            {"presence": "bogus", "idle_bucket": "0-5"},
        )
    assert excinfo.value.reason == "bad_payload"


def test_inprocess_command_and_query(gateway):
    status = gateway.tick_status()
    assert "tick_id" in status
    positions = gateway.positions()
    assert isinstance(positions, dict)


def test_inprocess_await_settled_pumps(gateway):
    # No tick loop runs in-process; await_settled must adjudicate
    # synchronously via the backend pump.
    record = gateway.submit_intent(
        "sess",
        "nonce-ipc-3",
        "tamer1",
        "tamer:tamer1",
        "tamer_presence",
        {"presence": "active", "idle_bucket": "0-5"},
    )
    assert record["status"] == "pending"
    fresh = gateway.await_settled("sess", "nonce-ipc-3", timeout=5.0)
    assert fresh["status"] in ("adjudicated", "rejected")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_tcp_round_trip():
    port = _free_port()
    server = SimServer(SimDispatcher(), port=port)
    server.start()
    try:
        client = SimClient(port=port)
        resp = client.call({"type": "ping"}, timeout=5.0)
        assert resp["ok"] is True
        # Intent submit over the wire, then a status read.
        resp = client.call(
            {
                "type": "intent_submit",
                "session_id": "sess",
                "nonce": "nonce-tcp-1",
                "custodian_id": "tamer1",
                "soul_id": "tamer:tamer1",
                "kind": "tamer_presence",
                "payload": {"presence": "active", "idle_bucket": "0-5"},
            },
            timeout=5.0,
        )
        assert resp["ok"] is True
        assert resp["record"]["nonce"] == "nonce-tcp-1"
        # Unknown command -> structured error, not a hang.
        resp = client.call(
            {"type": "command", "name": "nope", "params": {}}, timeout=5.0
        )
        assert resp["ok"] is False
    finally:
        server.stop()


def test_client_retries_once_on_stale_connection():
    port = _free_port()
    server = SimServer(SimDispatcher(), port=port)
    server.start()
    try:
        client = SimClient(port=port)
        assert client.call({"type": "ping"}, timeout=5.0)["ok"] is True
        stale = client._local.sock
        stale.close()
        resp = client.call({"type": "ping"}, timeout=5.0)
        assert resp["ok"] is True
        assert client._local.sock is not stale
    finally:
        server.stop()


def test_retry_of_intent_submit_is_nonce_idempotent(db_conn):
    port = _free_port()
    server = SimServer(SimDispatcher(), port=port)
    server.start()
    try:
        client = SimClient(port=port)
        first = client.call(
            {
                "type": "intent_submit",
                "session_id": "sess",
                "nonce": "nonce-retry-1",
                "custodian_id": "tamer1",
                "soul_id": "tamer:tamer1",
                "kind": "tamer_presence",
                "payload": {"presence": "active", "idle_bucket": "0-5"},
            },
            timeout=5.0,
        )
        assert first["ok"] is True
        client._local.sock.close()
        second = client.call(
            {
                "type": "intent_submit",
                "session_id": "sess",
                "nonce": "nonce-retry-1",
                "custodian_id": "tamer1",
                "soul_id": "tamer:tamer1",
                "kind": "tamer_presence",
                "payload": {"presence": "active", "idle_bucket": "0-5"},
            },
            timeout=5.0,
        )
        assert second["ok"] is True
        assert second["created"] is False
        assert second["record"]["intent_id"] == first["record"]["intent_id"]
        rows = db_conn.execute(
            "SELECT COUNT(*) AS n FROM intents "
            "WHERE session_id = 'sess' AND nonce = 'nonce-retry-1'"
        ).fetchone()
        assert rows["n"] == 1
    finally:
        server.stop()


def test_server_idle_close_does_not_log_error(monkeypatch, caplog):
    import server.sim_ipc as ipc_module

    monkeypatch.setattr(ipc_module, "CALL_TIMEOUT_S", 0.2)
    port = _free_port()
    server = SimServer(SimDispatcher(), port=port)
    server.start()
    try:
        with caplog.at_level(logging.INFO, logger="soulscape_hub"):
            client = SimClient(port=port)
            client.call({"type": "ping"}, timeout=5.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any(
                    r.message == "sim connection idle, closing" for r in caplog.records
                ):
                    break
                time.sleep(0.05)
            errors = [
                r
                for r in caplog.records
                if r.levelno >= logging.ERROR and "sim connection" in r.message
            ]
            assert not errors
    finally:
        server.stop()
    client = SimClient(port=_free_port())  # nothing listening
    with pytest.raises(SimUnreachable):
        client.call({"type": "ping"}, timeout=2.0)


def test_sim_refusal_carries_ws_code():
    refusal = SimRefusal("plot_taken", "taken", ws_code="PLOT_TAKEN")
    assert refusal.ws_code == "PLOT_TAKEN"
    assert SimRefusal("x", "y").ws_code is None


def test_host_pump_returns_int_count():
    host = SimHost()
    assert isinstance(host.pump(), int)


def test_gateway_singleton_respects_env(monkeypatch):
    import server.sim_gateway as gw_module

    monkeypatch.setenv("SOULSCAPE_SIM_MODE", "inprocess")
    gw_module.reset_gateway()
    try:
        gw = gw_module.get_gateway()
        assert isinstance(gw.backend, InProcessBackend)
    finally:
        gw_module.reset_gateway()
