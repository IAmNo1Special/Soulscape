"""End-to-end viewport demo (issue #13 acceptance: hub-driven movement).

Spins up a real Hub with HUB_AUTHORITATIVE=1, creates a Soul with velocity
via REST, streams SNAPSHOT/DELTA frames over a live WebSocket into the
ViewportConsumer, steps a fake clock at 60 Hz, and asserts the rendered
positions track the Hub's integration smoothly, then snap instantly on a
teleport. Headless: no window, no pyglet.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
import urllib.request

import uvicorn
import websockets

from client.system.network.viewport_client import ViewportConsumer
from server import database as server_db
from server import main as server_main
from server import persistence as server_persistence

HUB_SECRET = "viewport-e2e-secret"
SOUL_ID = "viewport-e2e-soul"
VELOCITY_X = 120.0
MAX_RENDER_PX_PER_S = 2000.0
START_X, START_Y = 200.0, 300.0
SNAP_X, SNAP_Y = 1500.0, 800.0


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


def _hub_position() -> tuple[float, float]:
    return server_persistence.read_positions_through()[SOUL_ID]


class FakeClock:
    def __init__(self) -> None:
        self.t = time.monotonic()

    def __call__(self) -> float:
        return self.t


class TestViewportEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        server_db.DB_PATH = os.path.join(self._tmp.name, "e2e.db")
        server_db.init_db()

        os.environ["HUB_AUTHORITATIVE"] = "1"
        os.environ["SOULSCAPE_SIM_MODE"] = "inprocess"
        os.environ["HUB_SECRET_KEY"] = HUB_SECRET

        self.port = _free_port()
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

        from server.sim_gateway import get_gateway

        self._sim_tick = get_gateway().backend.host.tick
        await self._sim_tick.start()

        await _arest(
            self.port,
            "POST",
            "/souls",
            {
                "custodian_id": "e2e-owner",
                "souls": [
                    {
                        "soul_id": SOUL_ID,
                        "name": "ViewportE2E",
                        "position": [START_X, START_Y],
                        "velocity": [VELOCITY_X, 0.0],
                        "owner_id": "e2e-owner",
                    }
                ],
            },
        )

    async def asyncTearDown(self) -> None:
        await self._sim_tick.stop()
        self._server.should_exit = True
        await self._server_task
        self._tmp.cleanup()
        os.environ.pop("HUB_AUTHORITATIVE", None)
        os.environ.pop("HUB_SECRET_KEY", None)
        os.environ.pop("SOULSCAPE_SIM_MODE", None)

    async def _connect(self):
        ws = await websockets.connect(
            f"ws://127.0.0.1:{self.port}/ws/e2e-owner",
            additional_headers={"X-Hub-Secret": HUB_SECRET},
        )
        return ws

    async def test_hub_driven_movement_renders_smoothly(self) -> None:
        clock = FakeClock()
        consumer = ViewportConsumer(clock=clock)
        real_start = time.monotonic()
        clock.t = real_start

        async with await self._connect() as ws:
            snapshot = None
            for _ in range(20):
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                frame = json.loads(raw)
                if frame.get("type") == "snapshot":
                    snapshot = frame
                    break
            self.assertIsNotNone(snapshot, "no snapshot received")
            souls = {s["soul_id"]: s for s in snapshot["souls"]}
            self.assertIn(SOUL_ID, souls)
            consumer.apply_frame(snapshot)
            self.assertEqual(consumer.region[2:], (1920.0, 1080.0))

            deltas_seen = 0
            samples: list[tuple[float, float]] = []

            async def pump() -> None:
                nonlocal deltas_seen
                try:
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame.get("type") == "delta":
                            consumer.apply_frame(frame)
                            deltas_seen += 1
                except websockets.exceptions.ConnectionClosed:
                    pass

            pump_task = asyncio.create_task(pump())
            try:
                for _ in range(150):
                    await asyncio.sleep(1.0 / 60.0)
                    clock.t = real_start + (time.monotonic() - real_start)
                    pos = consumer.rendered_positions().get(SOUL_ID)
                    if pos is not None:
                        samples.append((clock.t, pos[0]))
            finally:
                pump_task.cancel()

        self.assertGreaterEqual(
            deltas_seen, 3, f"expected hub delta frames, got {deltas_seen}"
        )
        self.assertGreater(len(samples), 100)

        (_, first_x), (_, last_x) = samples[0], samples[-1]
        total = last_x - first_x
        elapsed = samples[-1][0] - samples[0][0]
        self.assertGreater(
            total,
            0.5 * VELOCITY_X * elapsed,
            f"soul barely moved: {total:.1f}px",
        )
        max_speed = 0.0
        for (t0, x0), (t1, x1) in zip(samples, samples[1:]):
            dt = t1 - t0
            if dt > 0.0:
                max_speed = max(max_speed, abs(x1 - x0) / dt)
        self.assertLess(
            max_speed,
            MAX_RENDER_PX_PER_S,
            f"render speed spike: {max_speed:.1f}px/s",
        )

        hub_x, _ = _hub_position()
        lag_px = hub_x - last_x
        self.assertGreater(
            lag_px,
            -(VELOCITY_X * 0.6),
            f"render lead {lag_px / VELOCITY_X:.2f}s exceeds budget",
        )
        self.assertLess(
            lag_px,
            VELOCITY_X * 0.6,
            f"render lag {lag_px / VELOCITY_X:.2f}s exceeds budget",
        )

    async def test_teleport_snaps_with_no_drift_ghosts(self) -> None:
        clock = FakeClock()
        consumer = ViewportConsumer(clock=clock)
        real_start = time.monotonic()
        clock.t = real_start

        async with await self._connect() as ws:
            for _ in range(20):
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                frame = json.loads(raw)
                if frame.get("type") == "snapshot":
                    consumer.apply_frame(frame)
                    break

            snap_frame = None

            async def pump() -> None:
                nonlocal snap_frame
                try:
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame.get("type") != "delta":
                            continue
                        consumer.apply_frame(frame)
                        for op in frame.get("ops", []):
                            if op.get("soul_id") == SOUL_ID and op.get("snap"):
                                snap_frame = frame
                                return
                except websockets.exceptions.ConnectionClosed:
                    pass

            pump_task = asyncio.create_task(pump())
            try:
                await _arest(
                    self.port,
                    "POST",
                    "/souls",
                    {
                        "custodian_id": "e2e-owner",
                        "souls": [
                            {
                                "soul_id": SOUL_ID,
                                "position": [SNAP_X, SNAP_Y],
                                "velocity": [0.0, 0.0],
                            }
                        ],
                    },
                )
                for _ in range(100):
                    if snap_frame is not None:
                        break
                    await asyncio.sleep(0.05)
                self.assertIsNotNone(snap_frame, "no snap delta received")

                clock.t = real_start + (time.monotonic() - real_start)
                pos = consumer.rendered_positions()[SOUL_ID]
                self.assertAlmostEqual(pos[0], SNAP_X, delta=1.0)
                self.assertAlmostEqual(pos[1], SNAP_Y, delta=1.0)

                for _ in range(30):
                    await asyncio.sleep(1.0 / 60.0)
                    clock.t = real_start + (time.monotonic() - real_start)
                    pos = consumer.rendered_positions()[SOUL_ID]
                    self.assertAlmostEqual(pos[0], SNAP_X, delta=2.0)
                    self.assertAlmostEqual(pos[1], SNAP_Y, delta=2.0)
            finally:
                pump_task.cancel()


if __name__ == "__main__":
    unittest.main()
