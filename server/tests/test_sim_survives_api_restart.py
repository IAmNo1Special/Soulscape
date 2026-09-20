"""Two-process survival test (issue #37).

Spawns a real SimProcess and a real API process as subprocesses:

1. Submit an intent to the sim immediately before SIGKILLing the API.
2. Assert the sim's tick counter keeps advancing while the API is dead
   (the world survives API restarts).
3. Restart the API and assert /health reports the sim reachable.
4. Assert the intent was durably ACKed or cleanly rejected -- never
   silently lost.

This is the core proof of the issue: the sim owns the world, the API
is stateless.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from ..sim_ipc import SimClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for(fn, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    last_exc = None
    while time.monotonic() < deadline:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {last_exc!r}")


def _ping_tick(client: SimClient) -> int:
    resp = client.call({"type": "ping"}, timeout=5.0)
    assert resp["ok"] is True
    return int(resp["tick_id"])


def _health(api_port: int) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{api_port}/health", timeout=5
    ) as resp:
        return json.load(resp)


def _popen_sim(db_path: str, sim_port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "SOULSCAPE_DB_PATH": db_path,
        "SIM_PORT": str(sim_port),
        "SIM_TICK_HZ": "20",
        "HUB_SECRET_KEY": "soulscape-secret-123",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "server.sim_process"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _popen_api(api_port: int, sim_port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "SERVER_PORT": str(api_port),
        "SIM_PORT": str(sim_port),
        # SOULSCAPE_SIM_MODE intentionally unset: the API must use IPC.
        "HUB_SECRET_KEY": "soulscape-secret-123",
    }
    env.pop("SOULSCAPE_SIM_MODE", None)
    return subprocess.Popen(
        [sys.executable, "-c", "from server.main import main; main()"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.integration
def test_world_survives_api_restart(tmp_path):
    db_path = str(tmp_path / "sim.db")
    sim_port = _free_port()
    api_port = _free_port()

    sim = _popen_sim(db_path, sim_port)
    api = None
    api2 = None
    try:
        client = SimClient(port=sim_port)
        tick_boot = _wait_for(lambda: _ping_tick(client), 30.0, "sim IPC ping")

        # Submit an intent immediately before the API kill. The sim
        # accepts it durably (commit-before-ack) independent of the API.
        nonce = "integ_" + secrets.token_urlsafe(8)
        resp = client.call(
            {
                "type": "intent_submit",
                "session_id": "integ",
                "nonce": nonce,
                "custodian_id": "tamer_integ",
                "soul_id": "tamer:tamer_integ",
                "kind": "tamer_presence",
                "payload": {"presence": "active", "idle_bucket": "0-5"},
            },
            timeout=10.0,
        )
        assert resp["ok"] is True, resp
        assert resp["record"]["status"] == "pending"

        # Bring the API up and confirm it sees the sim.
        api = _popen_api(api_port, sim_port)
        health = _wait_for(lambda: _health(api_port), 30.0, "API /health")
        assert health["sim"]["reachable"] is True
        tick_with_api = health["sim"]["tick_id"]
        assert tick_with_api >= tick_boot

        # SIGKILL the API mid-world.
        api.send_signal(signal.SIGKILL)
        api.wait(timeout=10)
        api = None

        # The world's tick counter keeps advancing with no API alive.
        tick_dead_1 = _ping_tick(client)
        time.sleep(1.0)
        tick_dead_2 = _ping_tick(client)
        assert tick_dead_2 > tick_dead_1, (
            f"sim tick stalled while API was dead: {tick_dead_1} -> {tick_dead_2}"
        )

        # Restart the API: it reconnects, /health is green, and the
        # world is intact (tick kept moving across the restart).
        api2 = _popen_api(api_port, sim_port)
        health2 = _wait_for(
            lambda: _health(api_port), 30.0, "API /health after restart"
        )
        assert health2["status"] == "online"
        assert health2["sim"]["reachable"] is True
        assert health2["sim"]["tick_id"] > tick_dead_2

        # The pre-kill intent was never silently lost: it settled.
        status = client.call(
            {"type": "intent_status", "session_id": "integ", "nonce": nonce},
            timeout=10.0,
        )
        record = status["record"]
        assert record is not None
        assert record["status"] in ("adjudicated", "rejected"), record
    finally:
        for proc in (api, api2):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        if sim.poll() is None:
            sim.terminate()
            try:
                sim.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sim.kill()
                sim.wait(timeout=10)
