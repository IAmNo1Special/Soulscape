"""End-to-end intent ingress proof (issue #14 acceptance).

Spins up a real Hub with HUB_AUTHORITATIVE=1 and drives the REAL client
PresenceManager.send_intent over a live WebSocket, then asserts the full
acceptance surface: signed intent -> durable ACK -> tick adjudication ->
movement -> viewport DELTA; duplicate-nonce idempotency; forged-Soul
rejection; far-target clamp; and startup recovery of a pending intent
across a Hub restart. Headless: no window, no pyglet.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import tempfile
import unittest
import urllib.request

import uvicorn
import websockets

from client.system.network.presence import PresenceManager
from server import database as server_db
from server import intents as server_intents
from server import main as server_main
from server import persistence as server_persistence

HUB_SECRET = "soulscape-secret-123"
TAMER = "intent-e2e-tamer"
OTHER_TAMER = "intent-e2e-other"
SOUL_ID = "intent-e2e-soul"
OTHER_SOUL_ID = "intent-e2e-other-soul"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _rest(port: int, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Secret": HUB_SECRET,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


async def _arest(port: int, method: str, path: str, payload: dict) -> dict:
    return await asyncio.to_thread(_rest, port, method, path, payload)


def _soul_row(soul_id: str) -> dict:
    with server_db.get_db() as conn:
        row = conn.execute(
            "SELECT position, move_target FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    return dict(row)


def _intent_row(session_id: str, nonce: str) -> dict | None:
    with server_db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM intents WHERE session_id = ? AND nonce = ?",
            (session_id, nonce),
        ).fetchone()
    return dict(row) if row is not None else None


def _intent_count(session_id: str, nonce: str) -> int:
    with server_db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM intents WHERE session_id = ? AND nonce = ?",
            (session_id, nonce),
        ).fetchone()
    return int(row["n"])


def _rt_position(soul_id: str) -> tuple[float, float]:
    """Authoritative position: dirty write-behind overlaid on the DB.

    The tick integrates into an in-memory dirty set flushed every few
    seconds, so a raw DB read is stale mid-flight. This is the same
    read-through path the viewport and REST readers use.
    """
    return server_persistence.read_positions_through()[soul_id]


def _spawn_positions(resp: dict) -> dict[str, tuple[float, float]]:
    return {
        s["soul_id"]: (float(s["position"][0]), float(s["position"][1]))
        for s in resp.get("souls", [])
    }


def _signed_frame(
    key: str, session_id: str, nonce: str, soul_id: str, **fields
) -> dict:
    frame = {
        "v": 1,
        "type": "intent",
        "kind": "move_to",
        "nonce": nonce,
        "session_id": session_id,
        "soul_id": soul_id,
        **fields,
    }
    frame["signature"] = server_intents.sign_intent(frame, key)
    return frame


class TestIntentEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        server_db.DB_PATH = os.path.join(self._tmp.name, "intent-e2e.db")
        server_db.init_db()

        os.environ["HUB_AUTHORITATIVE"] = "1"
        os.environ["SOULSCAPE_SIM_MODE"] = "inprocess"
        os.environ["HUB_SECRET_KEY"] = HUB_SECRET
        self.port = _free_port()
        os.environ["HUB_URL"] = f"http://127.0.0.1:{self.port}"

        await self._start_hub()

        # Newborn souls spawn at the Commons center (issue #41); the hub
        # echoes the actual stored position, which the tests drive from
        # instead of assuming the requested coordinates.
        resp = await _arest(
            self.port,
            "POST",
            "/souls",
            {
                "custodian_id": TAMER,
                "souls": [
                    {
                        "soul_id": SOUL_ID,
                        "name": "IntentE2E",
                        "position": [100.0, 100.0],
                        "velocity": [0.0, 0.0],
                        "owner_id": TAMER,
                    },
                ],
            },
        )
        resp2 = await _arest(
            self.port,
            "POST",
            "/souls",
            {
                "custodian_id": OTHER_TAMER,
                "souls": [
                    {
                        "soul_id": OTHER_SOUL_ID,
                        "name": "IntentE2EOther",
                        "position": [500.0, 500.0],
                        "velocity": [0.0, 0.0],
                        "owner_id": OTHER_TAMER,
                    },
                ],
            },
        )
        self.spawn: dict[str, tuple[float, float]] = {
            **_spawn_positions(resp),
            **_spawn_positions(resp2),
        }
        self.assertIn(SOUL_ID, self.spawn, "hub did not echo spawn position")
        self.assertIn(OTHER_SOUL_ID, self.spawn, "hub did not echo spawn position")

        async def _noop_online(owner_id: str) -> None:
            return None

        def _noop_offline(owner_id: str) -> None:
            return None

        async def _noop_updated(souls: list, owner_id: str) -> None:
            return None

        async def _on_frame(frame: dict) -> None:
            self.frames.append(frame)

        self.frames: list[dict] = []
        self.pm = PresenceManager(
            TAMER,
            on_owner_online=_noop_online,
            on_owner_offline=_noop_offline,
            on_soul_updated=_noop_updated,
            on_viewport_frame=_on_frame,
        )
        self._pm_task = asyncio.create_task(self.pm.connect())
        for _ in range(200):
            if self.pm._session_id and self.pm._hmac_key:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(
            self.pm._session_id and self.pm._hmac_key,
            "client never received a signed session",
        )

    async def _start_hub(self) -> None:
        config = uvicorn.Config(
            server_main.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self._server.started, "hub did not start")

        # Issue #37: the API no longer ticks. Run the sim loop in-process
        # (same dispatcher/messages as a real sim process) so intents
        # adjudicate and souls move.
        from server.sim_gateway import get_gateway

        self._sim_tick = get_gateway().backend.host.tick
        await self._sim_tick.start()

    async def _stop_hub(self) -> None:
        await self._sim_tick.stop()
        self._server.should_exit = True
        await self._server_task

    async def asyncTearDown(self) -> None:
        await self.pm.disconnect()
        self._pm_task.cancel()
        await self._stop_hub()
        self._tmp.cleanup()
        for var in (
            "HUB_AUTHORITATIVE",
            "HUB_SECRET_KEY",
            "HUB_URL",
            "SOULSCAPE_SIM_MODE",
        ):
            os.environ.pop(var, None)

    async def _raw_connect(self, owner: str = TAMER):
        ws = await websockets.connect(
            f"ws://127.0.0.1:{self.port}/ws/{owner}",
            additional_headers={"X-Hub-Secret": HUB_SECRET},
        )
        connected = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
        assert connected["type"] == "connected"
        for _ in range(10):
            frame = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
            if frame.get("type") == "snapshot":
                break
        return ws, connected

    async def _wait_for(self, check, timeout: float = 10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = check()
            if result:
                return result
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("timed out waiting for condition")
            await asyncio.sleep(0.1)

    async def test_signed_intent_ack_adjudication_and_movement(self) -> None:
        sx, sy = self.spawn[SOUL_ID]
        # Target 400 px away along x, staying in-bounds and well inside the
        # 5 s intent horizon so adjudication does not clamp it.
        tx = sx + 400.0 if sx + 400.0 <= 1870.0 else sx - 400.0
        ty = sy
        ack = await self.pm.send_intent("move_to", SOUL_ID, x=tx, y=ty)
        self.assertIsNotNone(ack, "no ACK received")
        assert ack is not None
        self.assertEqual(ack["type"], "intent_ack")
        self.assertEqual(ack["status"], "accepted")
        self.assertTrue(ack["intent_id"])

        row = _intent_row(self.pm._session_id, ack["nonce"])
        self.assertIsNotNone(row, "intent not durable in DB at ACK time")

        def _adjudicated():
            row = _intent_row(self.pm._session_id, ack["nonce"])
            return row if row and row["status"] == "adjudicated" else None

        await self._wait_for(_adjudicated)
        soul = _soul_row(SOUL_ID)
        target = json.loads(soul["move_target"])
        self.assertEqual(target, [tx, ty])

        start_dist = math.hypot(tx - sx, ty - sy)

        def _moved():
            pos = _rt_position(SOUL_ID)
            return pos if math.hypot(pos[0] - sx, pos[1] - sy) > 50.0 else None

        pos = await self._wait_for(_moved)
        # Mid-flight: nearer the target than at spawn, never past it.
        self.assertLess(math.hypot(pos[0] - tx, pos[1] - ty), start_dist)

        def _arrived():
            pos = _rt_position(SOUL_ID)
            return pos if math.hypot(pos[0] - tx, pos[1] - ty) <= 5.0 else None

        await self._wait_for(_arrived)

        def _delta_seen():
            for frame in self.frames:
                if frame.get("type") != "delta":
                    continue
                for op in frame.get("ops", []):
                    if op.get("soul_id") == SOUL_ID:
                        state = op.get("state", {})
                        if (
                            math.hypot(
                                state.get("x", sx) - sx,
                                state.get("y", sy) - sy,
                            )
                            > 50.0
                        ):
                            return state
            return None

        seen = await self._wait_for(_delta_seen)
        self.assertGreater(math.hypot(seen["x"] - sx, seen["y"] - sy), 50.0)

    async def test_duplicate_nonce_is_idempotent(self) -> None:
        ws, connected = await self._raw_connect()
        try:
            session_id = connected["session_id"]
            key = connected["hmac_key"]
            frame = _signed_frame(
                key, session_id, "dup-nonce-1", SOUL_ID, x=300.0, y=300.0
            )
            await ws.send(json.dumps(frame))
            ack1 = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
            await ws.send(json.dumps(frame))
            ack2 = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
        finally:
            await ws.close()
        self.assertEqual(ack1["type"], "intent_ack")
        self.assertEqual(ack2["type"], "intent_ack")
        self.assertEqual(ack1["intent_id"], ack2["intent_id"])
        self.assertEqual(_intent_count(session_id, "dup-nonce-1"), 1)

    async def test_forged_soul_rejected(self) -> None:
        reg = await _arest(
            self.port,
            "POST",
            "/tamers/register",
            {"username": "forger", "password": "password123"},
        )
        tamer_id = reg["tamer_id"]
        login = await _arest(
            self.port,
            "POST",
            "/tamers/login",
            {"username": "forger", "password": "password123"},
        )
        token = login["token"]
        ws = await websockets.connect(
            f"ws://127.0.0.1:{self.port}/ws/{tamer_id}?token={token}",
        )
        try:
            connected = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
            assert connected["type"] == "connected"
            for _ in range(10):
                frame = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
                if frame.get("type") == "snapshot":
                    break
            frame = _signed_frame(
                connected["hmac_key"],
                connected["session_id"],
                "forged-1",
                OTHER_SOUL_ID,
                x=10.0,
                y=10.0,
            )
            await ws.send(json.dumps(frame))
            err = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
            self.assertEqual(err["type"], "error")
            self.assertEqual(err["code"], "CUSTODY_DENIED")
            self.assertEqual(_intent_count(connected["session_id"], "forged-1"), 0)
            # The forged intent moved nothing: the soul sits at its spawn.
            ox, oy = self.spawn[OTHER_SOUL_ID]
            pos = _rt_position(OTHER_SOUL_ID)
            self.assertAlmostEqual(pos[0], ox, delta=0.5)
            self.assertAlmostEqual(pos[1], oy, delta=0.5)
        finally:
            await ws.close()

    async def test_far_target_clamped(self) -> None:
        ack = await self.pm.send_intent("move_to", SOUL_ID, x=100000.0, y=100000.0)
        self.assertIsNotNone(ack)
        assert ack is not None

        def _adjudicated():
            row = _intent_row(self.pm._session_id, ack["nonce"])
            return row if row and row["status"] == "adjudicated" else None

        await self._wait_for(_adjudicated)
        sx, sy = self.spawn[SOUL_ID]
        target = json.loads(_soul_row(SOUL_ID)["move_target"])
        dist = math.hypot(target[0] - sx, target[1] - sy)
        self.assertLessEqual(dist, 3000.0 + 1.0)
        self.assertGreater(dist, 1000.0)

    async def test_pending_intent_survives_hub_restart(self) -> None:
        ack = await self.pm.send_intent("move_to", SOUL_ID, x=700.0, y=700.0)
        self.assertIsNotNone(ack)
        assert ack is not None
        first = _intent_row(self.pm._session_id, ack["nonce"])
        self.assertIsNotNone(first, "first intent not durable before restart")

        await self.pm.disconnect()
        self._pm_task.cancel()
        await self._stop_hub()

        pending_id = server_intents.enqueue_intent(
            session_id="recovery-session",
            nonce="recovery-nonce-1",
            custodian_id=TAMER,
            soul_id=SOUL_ID,
            kind="move_to",
            payload={"x": 1500.0, "y": 200.0},
        )
        self.assertTrue(pending_id)
        row = _intent_row("recovery-session", "recovery-nonce-1")
        self.assertEqual(row["status"], "pending")

        await self._start_hub()

        def _recovered():
            row = _intent_row("recovery-session", "recovery-nonce-1")
            return row if row and row["status"] == "adjudicated" else None

        await self._wait_for(_recovered)
        target = json.loads(_soul_row(SOUL_ID)["move_target"])
        self.assertEqual(target, [1500.0, 200.0])
        self.assertIsNotNone(
            _intent_row(self.pm._session_id, ack["nonce"]),
            "first intent lost across restart",
        )


if __name__ == "__main__":
    unittest.main()
