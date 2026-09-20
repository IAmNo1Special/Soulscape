"""Client-side expedition / abroad channel (issue #35).

The abroad channel streams 4-key summaries (entity_id, state,
activity_label, plot); the client drops the soul's track (no scene
rendering while away), keeps viewport identity for the tray, and
restores full rendering on abroad_end + snap move.
"""

from __future__ import annotations

import pytest

from shared import protocol

from client.system.bubble_config import DEFAULT_DURATIONS
from client.system.location import (
    AWAY_GLYPH,
    abroad_label,
    abroad_tooltip,
    walkoff_direction,
)
from client.system.network.viewport_client import ViewportConsumer
from client.ui.bubbles import KIND_EXPEDITION, BubbleManager
from client.ui.graphics.soul_uniforms import state_to_uniforms


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_snapshot(souls, region=None, seq=0, abroad=(), identities=None):
    frame = protocol.envelope(
        protocol.MessageType.SNAPSHOT,
        seq=seq,
        tick_id=1,
        souls=[{"soul_id": sid, "x": x, "y": y} for sid, x, y in souls],
    )
    if identities:
        for entry, ident in zip(frame["souls"], identities):
            entry.update(ident)
    if region is not None:
        frame["region"] = region
    if abroad:
        frame["abroad"] = list(abroad)
    return frame


def make_delta(ops, seq=1):
    return protocol.envelope(protocol.MessageType.DELTA, seq=seq, tick_id=1, ops=ops)


def abroad_op(soul_id, state="abroad", activity="exploring", plot="7:4", **extra):
    return {
        "op": "abroad",
        "soul_id": soul_id,
        "entity_id": soul_id,
        "state": state,
        "activity_label": activity,
        "plot": plot,
        **extra,
    }


def abroad_end_op(soul_id):
    return {"op": "abroad_end", "soul_id": soul_id}


def upsert_op(soul_id, x, y, snap=False):
    op = {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "state": {"soul_id": soul_id, "x": x, "y": y},
    }
    if snap:
        op["snap"] = True
    return op


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def consumer(clock):
    return ViewportConsumer(clock=clock)


def _tracked(consumer, clock, sid="s1", x=100.0, y=100.0):
    consumer.apply_frame(make_snapshot([(sid, x, y)]))
    clock.advance(0.3)
    assert sid in consumer.rendered_positions()


class TestAbroadChannel:
    def test_abroad_op_removes_from_rendered_positions(self, consumer, clock):
        _tracked(consumer, clock)
        consumer.apply_frame(make_delta([abroad_op("s1")]))
        clock.advance(0.3)
        # No scene rendering while fully abroad.
        assert "s1" not in consumer.rendered_positions()
        assert consumer.is_abroad("s1")
        assert consumer.abroad_soul_ids() == ["s1"]

    def test_abroad_summary_exact_four_keys(self, consumer, clock):
        _tracked(consumer, clock)
        summary = consumer.abroad_summary("s1")
        assert summary is None
        consumer.apply_frame(
            make_delta(
                [
                    abroad_op(
                        "s1",
                        state="abroad",
                        activity="foraging",
                        plot="7:4",
                        # A compromised verbose server payload: none of
                        # these may land in the stored summary.
                        x=961.5,
                        y=541.2,
                        name="Brave",
                        biology={"satiety": 88.1},
                        viewport={"x": 0, "y": 0},
                    )
                ]
            )
        )
        stored = consumer.abroad_summary("s1")
        assert set(stored.keys()) == {
            "entity_id",
            "state",
            "activity_label",
            "plot",
        }
        assert stored == {
            "entity_id": "s1",
            "state": "abroad",
            "activity_label": "foraging",
            "plot": "7:4",
        }

    def test_abroad_end_plus_snap_restores_full_rendering(self, consumer, clock):
        _tracked(consumer, clock, x=100.0, y=100.0)
        consumer.apply_frame(make_delta([abroad_op("s1", plot="7:4")]))
        clock.advance(0.3)
        assert "s1" not in consumer.rendered_positions()
        # Return: abroad_end first, then the snap move in the same frame.
        consumer.apply_frame(
            make_delta([abroad_end_op("s1"), upsert_op("s1", 960.0, 540.0, snap=True)])
        )
        clock.advance(0.3)
        assert not consumer.is_abroad("s1")
        pos = consumer.rendered_positions()["s1"]
        assert pos == pytest.approx((960.0, 540.0))
        assert consumer.abroad_summary("s1") is None

    def test_identity_preserved_for_tray_across_abroad(self, consumer, clock):
        consumer.apply_frame(
            make_snapshot(
                [("s1", 100.0, 100.0)],
                identities=[{"name": "Brave", "species": "Wisp"}],
            )
        )
        assert consumer.soul_identity("s1")["name"] == "Brave"
        consumer.apply_frame(make_delta([abroad_op("s1", plot="7:4")]))
        # Tray tooltip keeps the name even though the track is gone.
        assert consumer.soul_identity("s1")["name"] == "Brave"

    def test_snapshot_abroad_list_without_soul_entry(self, consumer, clock):
        # Fresh client: soul is already away when the first snapshot
        # arrives. No track, but the away state is known.
        consumer.apply_frame(
            make_snapshot(
                [],
                abroad=[
                    {
                        "entity_id": "s1",
                        "state": "abroad",
                        "activity_label": "exploring",
                        "plot": "6:4",
                    }
                ],
            )
        )
        assert consumer.is_abroad("s1")
        assert "s1" not in consumer.rendered_positions()
        # Unknown identity degrades to the id prefix, never crashes.
        assert consumer.soul_identity("s1")["name"] == "s1"


class TestAwayStrings:
    def test_abroad_label_exact(self):
        assert (
            abroad_label({"plot": "7:4", "activity_label": "foraging"})
            == f"{AWAY_GLYPH} abroad — plot 7:4, foraging"
        )
        assert AWAY_GLYPH == "✈"

    def test_abroad_tooltip_exact(self):
        assert (
            abroad_tooltip("Brave", {"plot": "7:3", "activity_label": "foraging"})
            == "Brave is abroad — plot 7:3, foraging"
        )

    def test_walkoff_direction_toward_nearest_edge(self):
        w, h = 1920.0, 1080.0
        # Left of center drifts toward the left edge, never back inside.
        dx, dy = walkoff_direction(200.0, 540.0, w, h)
        assert dx < 0.0 and dy == 0.0
        # Right of center drifts toward the right edge.
        dx, dy = walkoff_direction(1700.0, 540.0, w, h)
        assert dx > 0.0 and dy == 0.0
        # Above center drifts up; dominant axis wins.
        dx, dy = walkoff_direction(960.0, 100.0, w, h)
        assert dx == 0.0 and dy < 0.0
        # Below center drifts down.
        dx, dy = walkoff_direction(960.0, 900.0, w, h)
        assert dx == 0.0 and dy > 0.0


class TestExpeditionBubbles:
    def _manager(self):
        return BubbleManager()

    def test_expedition_kind_accepted(self):
        mgr = self._manager()
        result = mgr.show_bubble(
            "s1", "Off to explore plot 4:4!", kind=KIND_EXPEDITION, solicited=True
        )
        assert result.accepted
        assert KIND_EXPEDITION == "expedition"

    def test_expedition_bubble_duration_six_seconds(self):
        assert DEFAULT_DURATIONS["expedition"] == 6.0
        mgr = self._manager()
        before = 1000.0
        result = mgr.show_bubble(
            "s1",
            "Back home from plot 4:4!",
            kind=KIND_EXPEDITION,
            solicited=True,
            now=before,
        )
        assert result.accepted
        bubble = result.bubble
        assert bubble.expires_at == pytest.approx(before + 6.0)


class TestFadeAlphaPlumbing:
    def _state(self, **overrides):
        state = {
            "satiety": 1.0,
            "hydration": 1.0,
            "hp": 1.0,
            "statue_kind": None,
            "typing_dip": 0.0,
            "reflex": None,
            "reflex_t": 0.0,
            "base_color": (0.9, 0.2, 0.3),
        }
        state.update(overrides)
        return state

    def test_fade_alpha_scales_opacity(self):
        full = state_to_uniforms(self._state(fade_alpha=1.0))
        half = state_to_uniforms(self._state(fade_alpha=0.5))
        gone = state_to_uniforms(self._state(fade_alpha=0.0))
        assert half["opacity"] == pytest.approx(full["opacity"] * 0.5)
        assert gone["opacity"] == pytest.approx(0.0)

    def test_fade_alpha_defaults_to_one(self):
        uniforms = state_to_uniforms(self._state())
        assert uniforms["opacity"] > 0.0
