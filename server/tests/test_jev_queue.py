import pytest

from ..agents.jev_queue import JevEventQueue


@pytest.fixture
def queue():
    return JevEventQueue()


def snap(soul_id="s1"):
    return {"soul_id": soul_id, "position": [0.0, 0.0]}


def test_first_note_is_immediately_due(queue):
    queue.note("s1", "restlessness", 100.0, snap())
    job = queue.pump(100.0)
    assert job is not None
    assert job.soul_id == "s1"
    assert job.lane == 2
    assert job.kinds == ["restlessness"]
    assert job.snapshot == snap()
    assert job.snapshot_at == 100.0


def test_five_second_floor_between_thinks(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    assert queue.pump(100.0) is not None
    queue.note("s1", "vision_enter", 101.0, snap())
    assert queue.pump(104.9) is None
    job = queue.pump(105.0)
    assert job is not None
    assert job.soul_id == "s1"


def test_tamer_floor_one_second(queue):
    queue.note("s1", "tamer_poke", 100.0, snap())
    assert queue.pump(100.0) is not None
    queue.note("s1", "tamer_poke", 100.5, snap())
    assert queue.pump(100.9) is None
    assert queue.pump(101.0) is not None


def test_tamer_poke_preempts_normal_deadline_and_wakes(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    assert queue.pump(100.0) is not None
    queue.note("s1", "vision_enter", 101.0, snap())
    assert queue.earliest_deadline() == 105.0
    woke = queue.note("s1", "tamer_poke", 102.0, snap())
    assert woke is True
    assert queue.earliest_deadline() == 102.0
    job = queue.pump(102.0)
    assert job.lane == 0
    assert sorted(job.kinds) == ["tamer_poke", "vision_enter"]


def test_kinds_coalesce_into_one_job(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    assert queue.pump(100.0) is not None
    queue.note("s1", "vision_enter", 101.0, snap())
    queue.note("s1", "wallet_delta", 102.0, snap())
    job = queue.pump(106.0)
    assert job is not None
    assert job.kinds == ["vision_enter", "wallet_delta"]


def test_replace_newest_snapshot_wins(queue):
    queue.note("s1", "vision_enter", 100.0, {"v": 1})
    assert queue.pump(100.0) is not None
    queue.note("s1", "vision_enter", 101.0, {"v": 2})
    job = queue.pump(106.0)
    assert job.snapshot == {"v": 2}
    assert job.enqueued_at == 101.0


def test_lane_priority_strict_single_pump(queue):
    queue.note("slow", "restlessness", 100.0, snap("slow"))
    queue.note("mid", "vision_enter", 100.0, snap("mid"))
    queue.note("fast", "tamer_poke", 100.0, snap("fast"))
    assert queue.pump(100.0).soul_id == "fast"
    assert queue.pump(100.0).soul_id == "mid"
    assert queue.pump(100.0).soul_id == "slow"
    assert queue.pump(100.0) is None


def test_lane_priority_preempts_mid_batch(queue):
    queue.note("mid", "vision_enter", 100.0, snap("mid"))
    first = queue.pump(100.0)
    assert first.soul_id == "mid"
    queue.note("slow", "restlessness", 101.0, snap("slow"))
    queue.note("fast", "tamer_poke", 102.0, snap("fast"))
    assert queue.pump(102.0).soul_id == "fast"
    assert queue.pump(102.0).soul_id == "slow"


def test_fifo_within_lane(queue):
    queue.note("a", "vision_enter", 100.0, snap("a"))
    queue.note("b", "vision_enter", 100.0, snap("b"))
    queue.note("c", "vision_enter", 100.0, snap("c"))
    assert [queue.pump(100.0).soul_id for _ in range(3)] == ["a", "b", "c"]


def test_no_double_pump_without_new_note(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    assert queue.pump(100.0) is not None
    assert queue.pump(100.0) is None
    queue.note("s1", "vision_enter", 106.0, snap())
    assert queue.pump(106.0) is not None


def test_requeue_keeps_snapshot_and_backs_off(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    job = queue.pump(100.0)
    queue.requeue(job, 100.0)
    assert queue.has_pending("s1") is True
    assert queue.pump(100.4) is None
    again = queue.pump(100.5)
    assert again is not None
    assert again.snapshot == snap()


def test_requeue_merges_into_newer_note(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    job = queue.pump(100.0)
    queue.note("s1", "wallet_delta", 101.0, {"v": 9})
    queue.requeue(job, 101.0)
    merged = queue.pump(106.0)
    assert merged is not None
    assert sorted(merged.kinds) == ["vision_enter", "wallet_delta"]
    assert merged.snapshot == {"v": 9}


def test_forget_clears_state(queue):
    queue.note("s1", "tamer_poke", 100.0, snap())
    queue.forget("s1")
    assert queue.pump(200.0) is None
    assert queue.last_event_at("s1") is None
    assert queue.has_pending("s1") is False


def test_last_event_at_tracks_latest_note(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    queue.note("s1", "wallet_delta", 107.0, snap())
    assert queue.last_event_at("s1") == 107.0
    assert queue.has_pending("s1") is True


def test_unknown_kind_defaults_to_normal_lane(queue):
    queue.note("s1", "mystery_kind", 100.0, snap())
    job = queue.pump(100.0)
    assert job.lane == 1


def test_wander_continue_floor_override_dwell(queue):
    queue.note("s1", "vision_enter", 100.0, snap())
    assert queue.pump(100.0) is not None
    queue.note("s1", "wander_continue", 101.0, snap(), floor_override=3.0)
    assert queue.pump(103.9) is None
    assert queue.pump(104.0) is not None


def test_floor_uses_highest_priority_lane(queue):
    queue.note("s1", "restlessness", 100.0, snap())
    assert queue.pump(100.0) is not None
    queue.note("s1", "tamer_poke", 100.5, snap())
    assert queue.pump(100.9) is None
    assert queue.pump(101.0) is not None


def test_movement_complete_and_wander_continue_coalesce(queue):
    queue.note("s1", "movement_complete", 100.0, snap())
    queue.note("s1", "wander_continue", 100.0, snap(), floor_override=3.0)
    job = queue.pump(101.0)
    assert job is None
    job = queue.pump(103.0)
    assert job is not None
    assert job.kinds == ["movement_complete", "wander_continue"]
