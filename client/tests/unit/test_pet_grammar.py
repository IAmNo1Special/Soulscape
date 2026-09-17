"""Pet interaction grammar client tests (issue #31).

Headless: gesture detector (fake clock), gesture->intent mapping, info
card content, and viewport identity streaming. No window, no pyglet.
"""

from __future__ import annotations

import pytest

from client.core.interactions.pet_gestures import (
    CARRY_BEGIN,
    CARRY_END,
    CARRY_MOVE,
    CHIRP,
    DRAG_START_PX,
    HOLD_PET_THRESHOLD_S,
    PET,
    GestureEvent,
    PetGestureDetector,
    gesture_intent,
)
from client.core.interactions.pet_card import build_info_card, presence_status


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _detector():
    clock = FakeClock()
    return PetGestureDetector(clock=clock), clock


# ------------------------------------------------------------------
# gesture detector


def test_short_click_is_chirp():
    det, clock = _detector()
    det.press(10.0, 20.0)
    clock.advance(0.2)
    events = det.release(10.0, 20.0)
    assert [e.kind for e in events] == [CHIRP]


def test_hold_past_threshold_is_pet_via_poll():
    det, clock = _detector()
    det.press(10.0, 20.0)
    clock.advance(HOLD_PET_THRESHOLD_S + 0.05)
    events = det.poll()
    assert [e.kind for e in events] == [PET]
    # Poll fires exactly once; release after a fired pet is silent.
    assert det.poll() == []
    assert det.release(10.0, 20.0) == []


def test_hold_past_threshold_is_pet_on_release_without_poll():
    det, clock = _detector()
    det.press(10.0, 20.0)
    clock.advance(HOLD_PET_THRESHOLD_S + 0.5)
    events = det.release(10.0, 20.0)
    assert [e.kind for e in events] == [PET]


def test_small_jitter_below_drag_threshold_stays_chirp():
    det, clock = _detector()
    det.press(10.0, 20.0)
    assert det.drag(10.0 + DRAG_START_PX - 1.0, 20.0) == []
    clock.advance(0.1)
    events = det.release(10.0 + DRAG_START_PX - 1.0, 20.0)
    assert [e.kind for e in events] == [CHIRP]


def test_drag_becomes_carry_grab_move_release():
    det, clock = _detector()
    det.press(10.0, 20.0)
    events = det.drag(10.0 + DRAG_START_PX + 1.0, 20.0)
    assert [e.kind for e in events] == [CARRY_BEGIN]
    # Streamed moves respect the bounded cadence.
    assert det.drag(30.0, 40.0) == []
    clock.advance(0.25)
    moves = det.drag(30.0, 40.0)
    assert [e.kind for e in moves] == [CARRY_MOVE]
    assert moves[0].x == 30.0 and moves[0].y == 40.0
    events = det.release(30.0, 40.0)
    assert [e.kind for e in events] == [CARRY_END]


def test_long_hold_then_drag_is_carry_not_pet():
    det, clock = _detector()
    det.press(10.0, 20.0)
    clock.advance(HOLD_PET_THRESHOLD_S + 1.0)
    events = det.drag(10.0 + DRAG_START_PX + 5.0, 20.0)
    assert [e.kind for e in events] == [CARRY_BEGIN]
    assert det.poll() == []


def test_drag_after_pet_fired_stays_pet():
    # One press yields one gesture: once poll() has emitted the pet,
    # a later drag cannot re-interpret the press as a carry.
    det, clock = _detector()
    det.press(10.0, 20.0)
    clock.advance(HOLD_PET_THRESHOLD_S + 0.1)
    assert [e.kind for e in det.poll()] == [PET]
    assert det.drag(10.0 + DRAG_START_PX + 5.0, 20.0) == []
    assert det.release(50.0, 20.0) == []


def test_release_without_press_is_silent():
    det, _ = _detector()
    assert det.release(1.0, 2.0) == []
    assert det.drag(1.0, 2.0) == []
    assert det.poll() == []


# ------------------------------------------------------------------
# gesture -> intent mapping


def test_chirp_and_pet_map_to_affection_intents():
    kind, payload = gesture_intent(GestureEvent(CHIRP, 1.0, 2.0), "s1")
    assert kind == "chirp"
    assert payload == {}

    kind, payload = gesture_intent(GestureEvent(PET, 1.0, 2.0), "s1")
    assert kind == "affection_pet"
    assert payload == {}


def test_carry_gestures_map_to_phased_carry_move():
    cases = [
        (CARRY_BEGIN, "grab"),
        (CARRY_MOVE, "move"),
        (CARRY_END, "release"),
    ]
    for gesture, phase in cases:
        kind, payload = gesture_intent(
            GestureEvent(gesture, 5.0, 6.0), "s1", (100.0, 200.0)
        )
        assert kind == "carry_move"
        assert payload == {"x": 100.0, "y": 200.0, "phase": phase}


def test_carry_without_world_coords_raises():
    with pytest.raises(ValueError):
        gesture_intent(GestureEvent(CARRY_BEGIN, 5.0, 6.0), "s1")


def test_no_direct_command_intent_is_ever_emitted():
    kinds = set()
    for gesture in (CHIRP, PET, CARRY_BEGIN, CARRY_MOVE, CARRY_END):
        kind, _ = gesture_intent(
            GestureEvent(gesture, 0.0, 0.0), "s1", (1.0, 1.0)
        )
        kinds.add(kind)
    assert kinds == {"chirp", "affection_pet", "carry_move"}
    assert "move_to" not in kinds


# ------------------------------------------------------------------
# info card


def _identity():
    return {
        "name": "Pip",
        "species": "Wisp",
        "level": 3,
        "activity": "playing",
    }


def _bio():
    return {"satiety": 82.0, "hydration": 64.0, "hp": 100.0, "max_hp": 100.0}


def test_info_card_shows_needs_activity_essence_whereabouts_presence():
    lines = build_info_card(
        "soul-abc", _identity(), _bio(), 42.5, "Plot 3:7", "online"
    )
    text = "\n".join(lines)
    assert "Pip" in text
    assert "Wisp" in text and "3" in text
    assert "playing" in text
    assert "satiety 82" in text
    assert "hydration 64" in text
    assert "essence: 42.5" in text
    assert "Plot 3:7" in text
    assert "presence: online" in text


def test_info_card_falls_back_without_identity():
    lines = build_info_card("soul-abcdef", None, None, None, "home", "offline")
    assert lines[0] == "soul-abc"
    assert "presence: offline" in lines[-1]


def test_presence_status_words():
    assert presence_status(False, False) == "offline"
    assert presence_status(False, True) == "offline"
    assert presence_status(True, False) == "online"
    assert presence_status(True, True) == "stale"


# ------------------------------------------------------------------
# viewport identity streaming


def _snapshot(soul_id: str, **fields):
    entry = {
        "soul_id": soul_id,
        "x": 10.0,
        "y": 20.0,
        "state": "normal",
        "dormant": False,
        "satiety": 80.0,
        "hydration": 70.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "name": "Pip",
        "species": "Wisp",
        "level": 4,
        "activity": "resting",
    }
    entry.update(fields)
    return {
        "type": "snapshot",
        "tick_id": 1,
        "reason": "connect",
        "souls": [entry],
        "tamer_souls": [],
        "economy": {},
        "wallets": {},
    }


def test_viewport_consumer_streams_identity():
    from client.system.network.viewport_client import ViewportConsumer

    consumer = ViewportConsumer()
    consumer.apply_frame(_snapshot("sid-1"))
    ident = consumer.soul_identity("sid-1")
    assert ident == {
        "name": "Pip",
        "species": "Wisp",
        "level": 4,
        "activity": "resting",
    }


def test_viewport_consumer_identity_falls_back_when_unknown():
    from client.system.network.viewport_client import ViewportConsumer

    consumer = ViewportConsumer()
    ident = consumer.soul_identity("nope")
    assert ident["name"] == "nope"[:8]
    assert ident["species"] == "Unknown"
    assert ident["level"] == 1
    assert ident["activity"] == "idle"


def test_viewport_consumer_clears_identity_on_remove():
    from client.system.network.viewport_client import ViewportConsumer

    consumer = ViewportConsumer()
    consumer.apply_frame(_snapshot("sid-9"))
    assert consumer.soul_identity("sid-9")["name"] == "Pip"
    consumer.apply_frame(
        {
            "type": "delta",
            "tick_id": 2,
            "ops": [
                {"op": "remove", "soul_id": "sid-9", "domain": "presence"}
            ],
        }
    )
    assert consumer.soul_identity("sid-9")["name"] == "sid-9"[:8]


def test_identity_delta_op_updates_viewport_state():
    from client.system.network.viewport_client import ViewportConsumer

    consumer = ViewportConsumer()
    consumer.apply_frame(_snapshot("sid-1"))
    assert consumer.soul_identity("sid-1")["activity"] == "resting"
    consumer.apply_frame(
        {
            "type": "delta",
            "ops": [
                {
                    "op": "upsert",
                    "soul_id": "sid-1",
                    "domain": "identity",
                    "state": {
                        "name": "Pip",
                        "species": "Wisp",
                        "level": 5,
                        "activity": "playing",
                    },
                }
            ],
        }
    )
    ident = consumer.soul_identity("sid-1")
    assert ident["activity"] == "playing"
    assert ident["level"] == 5
