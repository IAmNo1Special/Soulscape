"""Unit tests for the client viewport consumer (issue #13).

Pure logic: interpolation buffer, arc dead-reckoning, snap clearing, and
the uniform-scale plot-to-monitor mapping. No pyglet involved.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

import pytest

from shared import protocol

from client.core.commands import ViewportFrameCommand
from client.system.network.presence import PresenceManager
from client.system.network.viewport_client import (
    INTERP_DELAY_SECONDS,
    MAX_EXTRAPOLATE_SECONDS,
    ViewportConsumer,
    ViewportMapper,
    viewport_mode_enabled,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_snapshot(souls, region=None, seq=0):
    frame = protocol.envelope(
        protocol.MessageType.SNAPSHOT,
        seq=seq,
        tick_id=1,
        souls=[{"soul_id": sid, "x": x, "y": y} for sid, x, y in souls],
    )
    if region is not None:
        frame["region"] = region
    return frame


def make_delta(ops, seq=1):
    return protocol.envelope(protocol.MessageType.DELTA, seq=seq, tick_id=1, ops=ops)


def upsert_op(soul_id, x, y, snap=False):
    op = {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "state": {"soul_id": soul_id, "x": x, "y": y},
    }
    if snap:
        op["snap"] = True
    return op


def remove_op(soul_id):
    return {
        "op": protocol.EntityOpKind.REMOVE.value,
        "soul_id": soul_id,
        "state": None,
    }


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def consumer(clock):
    return ViewportConsumer(clock=clock)


class TestInterpolation:
    def test_render_lags_by_200ms(self, consumer, clock):
        assert INTERP_DELAY_SECONDS == pytest.approx(0.2)
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 20.0, 0.0)]))
        clock.advance(0.2)
        pos = consumer.rendered_positions()["s1"]
        assert pos == pytest.approx((20.0, 0.0))

    def test_linear_interpolation_between_samples(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 20.0, 10.0)]))
        pos = consumer.rendered_positions(now=clock.t + 0.1)["s1"]
        assert pos == pytest.approx((10.0, 5.0))

    def test_smooth_60fps_series_has_no_jitter(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        for i in range(1, 6):
            clock.advance(0.2)
            consumer.apply_frame(make_delta([upsert_op("s1", 20.0 * i, 0.0)]))
        xs = []
        t = clock.t - 0.8
        while t <= clock.t:
            xs.append(consumer.rendered_positions(now=t)["s1"][0])
            t += 1.0 / 60.0
        steps = [b - a for a, b in zip(xs, xs[1:])]
        assert all(s >= -1e-9 for s in steps)
        assert max(steps) == pytest.approx(100.0 / 60.0, rel=0.2)

    def test_single_sample_holds(self, consumer):
        consumer.apply_frame(make_snapshot([("s1", 5.0, 7.0)]))
        pos = consumer.rendered_positions()["s1"]
        assert pos == pytest.approx((5.0, 7.0))

    def test_unknown_frame_type_ignored(self, consumer):
        consumer.apply_frame({"v": 1, "type": "nope"})
        assert consumer.rendered_positions() == {}


class TestSnapHandling:
    def test_snap_op_clears_buffer_and_renders_instantly(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        for i in range(1, 4):
            clock.advance(0.2)
            consumer.apply_frame(make_delta([upsert_op("s1", 20.0 * i, 0.0)]))
        clock.advance(0.05)
        consumer.apply_frame(make_delta([upsert_op("s1", 500.0, 500.0, snap=True)]))
        pos = consumer.rendered_positions()["s1"]
        assert pos == pytest.approx((500.0, 500.0))
        track = consumer._tracks["s1"]
        assert len(track.samples) == 1

    def test_no_drift_ghosts_after_snap(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 100.0, 0.0, snap=True)]))
        for _ in range(10):
            clock.advance(1.0 / 60.0)
            pos = consumer.rendered_positions()["s1"]
            assert pos == pytest.approx((100.0, 0.0))

    def test_fresh_snapshot_replaces_souls(self, consumer):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0), ("s2", 9.0, 9.0)]))
        consumer.apply_frame(make_snapshot([("s2", 10.0, 10.0)]))
        assert consumer.soul_ids() == ["s2"]
        pos = consumer.rendered_positions()["s2"]
        assert pos == pytest.approx((10.0, 10.0))

    def test_snapshot_sets_region(self, consumer):
        consumer.apply_frame(
            make_snapshot(
                [("s1", 0.0, 0.0)],
                region={"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0},
            )
        )
        assert consumer.region == (0.0, 0.0, 1920.0, 1080.0)


class TestDeltaHandling:
    def test_remove_op_drops_soul(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0), ("s2", 1.0, 1.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([remove_op("s1")]))
        assert consumer.soul_ids() == ["s2"]

    def test_economy_upsert_without_xy_is_skipped(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 3.0, 4.0)]))
        clock.advance(0.2)
        consumer.apply_frame(
            make_delta(
                [
                    {
                        "op": protocol.EntityOpKind.UPSERT.value,
                        "soul_id": "s1",
                        "domain": "economy",
                        "state": {"soul_id": "s1", "essence": 42.0},
                    }
                ]
            )
        )
        pos = consumer.rendered_positions()["s1"]
        assert pos == pytest.approx((3.0, 4.0))

    def test_out_of_order_sample_resyncs(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.5)
        consumer.apply_frame(make_delta([upsert_op("s1", 50.0, 0.0)]))
        clock.advance(-0.4)
        consumer.apply_frame(make_delta([upsert_op("s1", 60.0, 0.0)]))
        assert len(consumer._tracks["s1"].samples) == 1


class TestDeadReckoning:
    def test_extrapolates_linear_motion(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 20.0, 0.0)]))
        pos = consumer.rendered_positions(now=clock.t + 0.4)["s1"]
        assert pos[0] == pytest.approx(40.0, rel=0.05)
        assert pos[1] == pytest.approx(0.0, abs=1e-6)

    def test_extrapolation_capped_at_400ms_then_holds(self, consumer, clock):
        assert MAX_EXTRAPOLATE_SECONDS == pytest.approx(0.4)
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 20.0, 0.0)]))
        at_cap = consumer.rendered_positions(now=clock.t + 0.6)["s1"]
        beyond = consumer.rendered_positions(now=clock.t + 2.0)["s1"]
        assert at_cap[0] == pytest.approx(20.0 + 100.0 * 0.4, rel=0.05)
        assert beyond == pytest.approx(at_cap)

    def test_arc_extrapolation_follows_curvature(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 0.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 20.0, 0.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 40.0, 4.0)]))
        pos = consumer.rendered_positions(now=clock.t + 0.4)["s1"]
        linear_y = 8.0
        assert pos[1] > linear_y
        assert pos[0] == pytest.approx(60.0, rel=0.1)

    def test_stationary_soul_holds(self, consumer, clock):
        consumer.apply_frame(make_snapshot([("s1", 7.0, 7.0)]))
        clock.advance(0.2)
        consumer.apply_frame(make_delta([upsert_op("s1", 7.0, 7.0)]))
        pos = consumer.rendered_positions(now=clock.t + 1.0)["s1"]
        assert pos == pytest.approx((7.0, 7.0))


class TestViewportMapper:
    def test_same_aspect_is_identity(self):
        mapper = ViewportMapper()
        mapper.set_region(0.0, 0.0, 1920.0, 1080.0)
        got = mapper.world_to_screen(960.0, 540.0, 1920.0, 1080.0)
        assert got == pytest.approx((960.0, 540.0))
        got = mapper.world_to_screen(0.0, 0.0, 1920.0, 1080.0)
        assert got == pytest.approx((0.0, 0.0))

    def test_slack_axis_shows_adjacent_world(self):
        mapper = ViewportMapper()
        mapper.set_region(0.0, 0.0, 1920.0, 1080.0)
        vis = mapper.visible_world(2560.0, 1080.0)
        assert vis == pytest.approx((-320.0, 0.0, 2560.0, 1080.0))
        got = mapper.world_to_screen(960.0, 540.0, 2560.0, 1080.0)
        assert got == pytest.approx((1280.0, 540.0))
        got = mapper.world_to_screen(-320.0, 0.0, 2560.0, 1080.0)
        assert got == pytest.approx((0.0, 0.0))

    def test_slack_axis_vertical(self):
        mapper = ViewportMapper()
        mapper.set_region(0.0, 0.0, 1920.0, 1080.0)
        vis = mapper.visible_world(1920.0, 1440.0)
        assert vis == pytest.approx((0.0, -180.0, 1920.0, 1440.0))

    def test_no_region_falls_back_to_monitor(self):
        mapper = ViewportMapper()
        assert mapper.visible_world(1920.0, 1080.0) == pytest.approx(
            (0.0, 0.0, 1920.0, 1080.0)
        )
        got = mapper.world_to_screen(100.0, 200.0, 1920.0, 1080.0)
        assert got == pytest.approx((100.0, 200.0))

    def test_reset_drops_region(self):
        mapper = ViewportMapper()
        mapper.set_region(0.0, 0.0, 1920.0, 1080.0)
        mapper.reset()
        assert mapper.region is None


class TestFlagSemantics:
    def test_viewport_mode_needs_flag_and_hub_url(self, monkeypatch):
        monkeypatch.setenv("HUB_AUTHORITATIVE", "1")
        monkeypatch.setenv("HUB_URL", "http://localhost:9785")
        assert viewport_mode_enabled() is True

    def test_flag_unset_disables(self, monkeypatch):
        monkeypatch.delenv("HUB_AUTHORITATIVE", raising=False)
        monkeypatch.setenv("HUB_URL", "http://localhost:9785")
        assert viewport_mode_enabled() is False

    def test_offline_disables_even_with_flag(self, monkeypatch):
        monkeypatch.setenv("HUB_AUTHORITATIVE", "1")
        monkeypatch.delenv("HUB_URL", raising=False)
        assert viewport_mode_enabled() is False


class FakeWS:
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class TestPresenceRouting(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_and_delta_routed_to_viewport_callback(self):
        on_viewport_frame = AsyncMock()
        manager = PresenceManager(
            owner_id="owner-1",
            on_owner_online=AsyncMock(),
            on_owner_offline=lambda oid: None,
            on_soul_updated=AsyncMock(),
            on_viewport_frame=on_viewport_frame,
        )
        snapshot = make_snapshot([("s1", 1.0, 2.0)])
        delta = make_delta([upsert_op("s1", 3.0, 4.0)])
        import json

        ws = FakeWS([json.dumps(snapshot), json.dumps(delta)])
        await manager._listen(ws)
        self.assertEqual(on_viewport_frame.await_count, 2)
        first = on_viewport_frame.await_args_list[0].args[0]
        self.assertEqual(first["type"], protocol.MessageType.SNAPSHOT.value)

    async def test_no_viewport_callback_does_not_raise(self):
        manager = PresenceManager(
            owner_id="owner-1",
            on_owner_online=AsyncMock(),
            on_owner_offline=lambda oid: None,
            on_soul_updated=AsyncMock(),
        )
        import json

        ws = FakeWS([json.dumps(make_snapshot([("s1", 1.0, 2.0)]))])
        await manager._listen(ws)


class TestViewportFrameCommand(unittest.IsolatedAsyncioTestCase):
    async def test_command_applies_frame_to_consumer(self):
        consumer = ViewportConsumer(clock=FakeClock())
        command = ViewportFrameCommand(
            consumer=consumer, frame=make_snapshot([("s1", 11.0, 22.0)])
        )
        command.execute(object())
        self.assertEqual(consumer.soul_ids(), ["s1"])
        self.assertEqual(consumer.rendered_positions()["s1"], (11.0, 22.0))

    async def test_command_without_consumer_is_noop(self):
        command = ViewportFrameCommand(
            consumer=None, frame=make_snapshot([("s1", 11.0, 22.0)])
        )
        command.execute(object())


if __name__ == "__main__":
    unittest.main()
