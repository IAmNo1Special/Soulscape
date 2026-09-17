"""Pet grammar end-to-end (issue #31 acceptance).

Spins up a real Hub with HUB_AUTHORITATIVE=1, drives the REAL client
PetGestureDetector with a fake clock, maps gestures to intents with
gesture_intent(), sends them over the live WebSocket via
PresenceManager.send_intent, and asserts adjudication outcomes:
chirp accepted + solicited "!" bubble arrives, pet nudges loyalty to
0.52, second pet is cooldown-rejected, carry grab/move/release
adjudicates. Headless: no window, no pyglet.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
import urllib.request

import uvicorn

from client.core.interactions.pet_gestures import (
    CARRY_BEGIN,
    CARRY_END,
    CARRY_MOVE,
    CHIRP,
    DRAG_START_PX,
    GestureEvent,
    PetGestureDetector,
    gesture_intent,
)
from client.system.network.presence import PresenceManager
from server import database as server_db
from server import main as server_main

HUB_SECRET = "e2e-pet-test-secret"
TAMER = "pet-e2e-tamer"
SOUL_ID = "pet-e2e-soul"
START_X, START_Y = 100.0, 100.0


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


def _intent_row(session_id: str, nonce: str) -> dict | None:
    with server_db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM intents WHERE session_id = ? AND nonce = ?",
            (session_id, nonce),
        ).fetchone()
    return dict(row) if row is not None else None


class FakeClock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestPetGrammarEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_db_path = server_db.DB_PATH
        self._prev_env = {
            var: os.environ.get(var)
            for var in ("HUB_AUTHORITATIVE", "HUB_SECRET_KEY", "HUB_URL")
        }
        server_db.DB_PATH = os.path.join(self._tmp.name, "pet-e2e.db")
        server_db.init_db()

        os.environ["HUB_AUTHORITATIVE"] = "1"
        os.environ["HUB_SECRET_KEY"] = HUB_SECRET
        self.port = _free_port()
        os.environ["HUB_URL"] = f"http://127.0.0.1:{self.port}"

        await self._start_hub()

        await _arest(
            self.port,
            "POST",
            "/souls",
            {
                "custodian_id": TAMER,
                "souls": [
                    {
                        "soul_id": SOUL_ID,
                        "name": "PetE2E",
                        "position": [START_X, START_Y],
                        "velocity": [0.0, 0.0],
                        "owner_id": TAMER,
                        "nature": "docile",
                    },
                ],
            },
        )

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

    async def _stop_hub(self) -> None:
        self._server.should_exit = True
        await self._server_task

    async def asyncTearDown(self) -> None:
        await self.pm.disconnect()
        self._pm_task.cancel()
        await self._stop_hub()
        self._tmp.cleanup()
        server_db.DB_PATH = self._prev_db_path
        for var, val in self._prev_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    async def _wait_for(self, check, timeout: float = 15.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = check()
            if result:
                return result
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("timed out waiting for condition")
            await asyncio.sleep(0.1)

    async def _send_gesture(
        self, event: GestureEvent, world_xy: tuple[float, float] | None = None
    ) -> dict:
        kind, payload = gesture_intent(event, SOUL_ID, world_xy)
        ack = await self.pm.send_intent(kind, SOUL_ID, **payload)
        self.assertIsNotNone(ack, "no ACK received")
        assert ack is not None
        self.assertEqual(ack["type"], "intent_ack")
        return ack

    def _settled(self, nonce: str):
        def _check():
            row = _intent_row(self.pm._session_id, nonce)
            if row and row["status"] in ("adjudicated", "rejected"):
                return row
            return None

        return _check

    async def test_chirp_gesture_adjudicates_and_bubble_arrives(self) -> None:
        clock = FakeClock()
        det = PetGestureDetector(clock=clock)
        det.press(50.0, 60.0)
        clock.advance(0.2)
        events = det.release(50.0, 60.0)
        self.assertEqual([e.kind for e in events], [CHIRP])
        ack = await self._send_gesture(events[0])
        self.assertEqual(ack["status"], "accepted")

        row = await self._wait_for(self._settled(ack["nonce"]))
        self.assertEqual(row["status"], "adjudicated")
        self.assertTrue(json.loads(row["result"])["chirped"])

        def _bubble_seen():
            for frame in self.frames:
                if frame.get("type") != "delta":
                    continue
                for op in frame.get("ops", []):
                    if (
                        op.get("op") == "bubble"
                        and op.get("soul_id") == SOUL_ID
                        and op.get("text") == "!"
                    ):
                        return op
            return None

        bubble = await self._wait_for(_bubble_seen)
        self.assertTrue(bubble["solicited"])

    async def test_pet_gesture_nudges_loyalty_then_cooldown_rejects(self) -> None:
        clock = FakeClock()
        det = PetGestureDetector(clock=clock)
        det.press(50.0, 60.0)
        clock.advance(1.0)
        events = det.poll()
        from client.core.interactions.pet_gestures import PET

        self.assertEqual([e.kind for e in events], [PET])
        ack = await self._send_gesture(events[0])
        row = await self._wait_for(self._settled(ack["nonce"]))
        self.assertEqual(row["status"], "adjudicated")
        self.assertAlmostEqual(json.loads(row["result"])["loyalty"], 0.52)

        # Happy feedback: the solicited heart bubble reaches the viewport.
        def _heart_seen():
            for frame in self.frames:
                if frame.get("type") != "delta":
                    continue
                for op in frame.get("ops", []):
                    if (
                        op.get("op") == "bubble"
                        and op.get("soul_id") == SOUL_ID
                        and op.get("text") == "\u2665"
                    ):
                        return op
            return None

        heart = await self._wait_for(_heart_seen)
        self.assertTrue(heart["solicited"])

        # Second pet inside the cooldown window is rejected server-side.
        det2 = PetGestureDetector(clock=clock)
        det2.press(50.0, 60.0)
        clock.advance(1.0)
        ack2 = await self._send_gesture(det2.poll()[0])
        row2 = await self._wait_for(self._settled(ack2["nonce"]))
        self.assertEqual(row2["status"], "rejected")
        self.assertEqual(json.loads(row2["result"])["reason"], "pet_cooldown")

    async def test_carry_gesture_flow_adjudicates(self) -> None:
        import unittest.mock as _mock
        # The hub adjudicates in-process: pin the escape roll off so the
        # happy path is deterministic (escape odds are covered by the
        # seeded server-side statistical tests).
        with _mock.patch("server.affection.roll_escape", return_value=False):
            await self._carry_flow()
            return

    async def _carry_flow(self) -> None:
        clock = FakeClock()
        det = PetGestureDetector(clock=clock)
        det.press(50.0, 60.0)
        world = (START_X, START_Y)

        grab_events = det.drag(50.0 + DRAG_START_PX + 2.0, 60.0)
        self.assertEqual([e.kind for e in grab_events], [CARRY_BEGIN])
        ack = await self._send_gesture(grab_events[0], world)
        row = await self._wait_for(self._settled(ack["nonce"]))
        self.assertEqual(row["status"], "adjudicated")
        grab_result = json.loads(row["result"])
        self.assertEqual(grab_result["phase"], "grab")
        # The soul may have wandered during hub setup; the move must be
        # relative to the true grab position, not the spawn point.
        gx, gy = grab_result["x"], grab_result["y"]
        dest = (gx + 10.0, gy + 5.0)

        clock.advance(0.3)
        move_events = det.drag(70.0, 80.0)
        self.assertEqual([e.kind for e in move_events], [CARRY_MOVE])
        ack = await self._send_gesture(move_events[0], dest)
        row = await self._wait_for(self._settled(ack["nonce"]))
        self.assertEqual(row["status"], "adjudicated")
        self.assertFalse(json.loads(row["result"])["escaped"])

        end_events = det.release(70.0, 80.0)
        self.assertEqual([e.kind for e in end_events], [CARRY_END])
        ack = await self._send_gesture(end_events[0], dest)
        row = await self._wait_for(self._settled(ack["nonce"]))
        self.assertEqual(row["status"], "adjudicated")
        self.assertTrue(json.loads(row["result"])["was_carried"])
