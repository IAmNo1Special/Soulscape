import time

import pytest
from goapauto.testing import FakeTypeSafeClient

from ..agents import brain_busy, jev
from ..agents.jev import JevBreaker, SensorRegistry
from ..agents.jev_queue import JevEventQueue
from ..agents.jev_worker import JevWorker


def _snapshot(**over):
    base = {
        "soul_id": "s1",
        "at": 1000.0,
        "position": [960.0, 540.0],
        "satiety": 90.0,
        "hydration": 90.0,
        "hp": 100.0,
        "fear": 0.0,
        "need_bands": {"satiety": "high", "hydration": "high", "hp": "high"},
        "state": "normal",
        "moving": False,
        "nearest_soul": None,
        "nearest_node": None,
        "has_food": False,
        "has_water": False,
        "node_nearby": False,
        "node_exists": False,
    }
    base.update(over)
    return base


def _answers(**over):
    base = {k: 0.0 for k in jev.QUESTIONS}
    base.update(over)
    return base


class Harness:
    def __init__(self, answers=None, snapshot=None, now=1000.0):
        self.now = [now]
        self.queue = JevEventQueue()
        self.breaker = JevBreaker(now_fn=lambda: self.now[0])
        self.sensors = SensorRegistry(
            FakeTypeSafeClient(answers=_answers(**(answers or {})))
        )
        self.emitted = []
        self.telemetry = []
        self.snapshot = _snapshot(**(snapshot or {}))
        self.suppress = False
        self.worker = JevWorker(
            queue=self.queue,
            breaker=self.breaker,
            sensors=self.sensors,
            emit_fn=lambda sid, goal, action, payload: self.emitted.append(
                (sid, goal, action, payload)
            ),
            telemetry_fn=self.telemetry.append,
            suppressed_fn=lambda sid, snap: self.suppress,
            now_fn=lambda: self.now[0],
        )

    def note(self, kinds=("restlessness",), at=1000.0, snapshot=None):
        for k in kinds:
            self.queue.note("s1", k, now=at, snapshot=dict(snapshot or self.snapshot))

    def pump(self, at):
        job = self.queue.pump(at)
        assert job is not None
        return job

    def process(self, kinds=("restlessness",), at=1000.0, snapshot=None):
        self.now[0] = at
        self.note(kinds, at, snapshot)
        self.worker._process_job(self.pump(at), self.now[0])


@pytest.fixture(autouse=True)
def clean_busy():
    brain_busy.reset()
    yield
    brain_busy.reset()


def test_happy_path_explore_emits_wander_leg():
    h = Harness()
    h.process()
    assert len(h.emitted) == 1
    sid, goal, action, payload = h.emitted[0]
    assert (sid, goal, action) == ("s1", "explore", "move_to")
    assert payload["pace"] == "amble"
    assert payload["wander"] is True
    assert "speed" not in payload
    assert 0.0 <= payload["x"] <= 1920.0
    assert 0.0 <= payload["y"] <= 1080.0
    assert len(h.telemetry) == 1
    row = h.telemetry[0]
    assert row.goal_selected == "explore"
    assert row.plan_action_count == 1
    assert row.error_type is None
    assert row.soul_id == "s1"


def test_sate_needs_emits_eat_directly():
    h = Harness(
        answers={"needs_pressing": 0.9},
        snapshot={"has_food": True, "satiety": 50.0},
    )
    h.process()
    assert h.emitted[0][1:3] == ("sate_needs", "eat")
    assert h.emitted[0][3] == {}


def test_flee_moves_away_at_default_speed():
    h = Harness(
        answers={"threatened": 0.9},
        snapshot={
            "position": [500.0, 500.0],
            "nearest_threat": {"position": [600.0, 500.0], "distance": 100.0},
        },
    )
    h.process()
    sid, goal, action, payload = h.emitted[0]
    assert goal == "flee_threat"
    assert payload["x"] < 500.0
    assert "speed" not in payload
    assert "wander" not in payload


def test_engage_user_emits_look():
    h = Harness(answers={"user_engaged": 0.9})
    h.process()
    assert h.emitted[0][1:3] == ("engage_user", "look")


def test_rest_emits_rest_and_marks_cooldown():
    h = Harness(answers={"should_rest": 0.9})
    h.process()
    assert len(h.emitted) == 1
    assert h.emitted[0][1:] == ("rest", "rest", {})
    row = h.telemetry[0]
    assert row.goal_selected == "rest"
    assert row.error_type is None


def test_below_threshold_falls_through():
    h = Harness(answers={"threatened": 0.4, "needs_pressing": 0.4})
    h.process()
    assert h.emitted[0][1] == "explore"


def test_missing_required_answer_means_unavailable():
    h = Harness()
    h.worker._judge = lambda sid, snap: (
        {
            "threatened": None,
            "needs_pressing": 0.9,
            "user_engaged": 0.0,
            "should_rest": 0.0,
        },
        None,
    )
    h.process()
    assert h.emitted == []
    assert h.telemetry[0].error_type == "judgment_unavailable"
    assert h.breaker.state == "closed"


def test_survival_shortcircuit_skips_judge():
    h = Harness(snapshot={"satiety": 10.0})
    h.process()
    assert h.emitted == []
    assert h.telemetry[0].error_type == "survival"
    assert h.sensors.get("s1").stats().calls == 0


def test_shed_old_job():
    h = Harness()
    h.note(at=1000.0)
    job = h.pump(1060.0)
    h.now[0] = 1020.0
    h.worker._process_job(job, h.now[0])
    assert h.emitted == []
    assert h.telemetry[0].error_type == "shed"
    assert h.telemetry[0].shed_count == 1


def test_shed_stale_snapshot():
    h = Harness()
    h.queue.note("s1", "restlessness", 1000.0, dict(h.snapshot), snapshot_at=900.0)
    job = h.pump(1000.0)
    h.worker._process_job(job, 916.0)
    assert h.emitted == []
    assert h.telemetry[0].error_type == "stale_snapshot"


def test_breaker_open_fails_fast_without_judge():
    h = Harness()
    for _ in range(3):
        h.breaker.on_failure()
    h.process()
    assert h.emitted == []
    assert h.telemetry[0].error_type == "breaker_open"
    assert h.sensors.get("s1").stats().calls == 0
    assert brain_busy.is_busy("s1") is False


def test_judge_timeout_trips_breaker_and_releases():
    from typesafe_sdk import TypeSafeAPITimeoutError

    def timeout(observation, questions):
        raise TypeSafeAPITimeoutError("timed out")

    h = Harness()
    h.sensors = SensorRegistry(FakeTypeSafeClient(responder=timeout))
    h.worker._sensors = h.sensors
    h.process()
    assert h.emitted == []
    assert h.telemetry[0].error_type == "TypeSafeAPITimeoutError"
    assert brain_busy.is_busy("s1") is False
    h.process(at=1006.0)
    h.process(at=1012.0)
    assert h.breaker.state == "open"


def test_unexpected_judge_error_does_not_trip_breaker():
    def boom(observation, questions):
        raise RuntimeError("bug")

    h = Harness()
    h.sensors = SensorRegistry(FakeTypeSafeClient(responder=boom))
    h.worker._sensors = h.sensors
    h.process()
    assert h.telemetry[0].error_type == "RuntimeError"
    assert h.breaker.state == "closed"
    assert brain_busy.is_busy("s1") is False


def test_inflight_cleared_on_success():
    h = Harness()
    h.process()
    assert brain_busy.is_busy("s1") is False
    assert "s1" not in h.worker._inflight


def test_brain_busy_contention_requeues_job():
    h = Harness()
    assert brain_busy.acquire("s1") is True
    try:
        h.process()
    finally:
        brain_busy.release("s1")
    assert h.emitted == []
    assert h.sensors.get("s1").stats().calls == 0
    assert h.queue.has_pending("s1") is True


def test_wander_suppressed_while_moving():
    h = Harness()
    h.suppress = True
    h.process()
    assert h.emitted == []
    assert h.telemetry[0].error_type == "wander_suppressed"


def test_wander_continues_across_legs():
    h = Harness()
    h.process()
    first = h.emitted[0][3]
    h.emitted.clear()
    arrived = dict(h.snapshot, position=[first["x"], first["y"]])
    h.worker.on_wander_leg_complete("s1", 1006.0, arrived)
    assert h.queue.has_pending("s1") is True
    dwell = h.worker._wander.dwell_for("s1")
    assert 2.0 <= dwell <= 5.0
    assert h.queue.pump(1006.0 + dwell - 0.1) is None
    assert h.queue.has_pending("s1") is True
    job = h.pump(1006.0 + dwell)
    h.worker._process_job(job, 1006.0 + dwell)
    second = h.emitted[0][3]
    assert (first["x"], first["y"]) != (second["x"], second["y"])


def test_wander_episode_ends_after_last_leg():
    h = Harness()
    h.process()
    now = 1010.0
    leg = None
    while True:
        assert h.emitted, "expected another wander leg"
        leg = h.emitted[0][3]
        h.emitted.clear()
        if not h.worker._wander.active("s1"):
            break
        dwell = h.worker._wander.dwell_for("s1")
        arrived = dict(h.snapshot, position=[leg["x"], leg["y"]])
        h.worker.on_wander_leg_complete("s1", now, arrived)
        assert h.queue.pump(now + dwell - 0.1) is None
        now += dwell
        h.worker._process_job(h.pump(now), now)
    assert h.worker._wander.active("s1") is False
    dwell = h.worker._wander.dwell_for("s1")
    arrived = dict(h.snapshot, position=[leg["x"], leg["y"]])
    h.worker.on_wander_leg_complete("s1", now, arrived)
    assert h.queue.pump(now + dwell - 0.1) is None
    h.worker._process_job(h.pump(now + dwell), now + dwell)
    assert h.emitted == []
    assert h.telemetry[-1].error_type == "episode_complete"


def test_higher_priority_goal_ends_wander_episode():
    h = Harness()
    h.process()
    assert h.worker._wander.active("s1") is True
    h2 = Harness(answers={"threatened": 0.9})
    h2.worker._wander = h.worker._wander
    brain_busy.reset()
    h2.process()
    assert h2.emitted[0][1] == "flee_threat"
    assert h.worker._wander.active("s1") is False


def test_forget_releases_everything():
    h = Harness()
    h.process()
    h.worker._cooldowns.mark("s1", "engage_user", 1000.0)
    brain_busy.acquire("s1")
    h.worker.forget("s1")
    assert brain_busy.is_busy("s1") is False
    assert h.worker._cooldowns.ready("s1", "engage_user", 1001.0) is True
    assert h.worker._wander.active("s1") is False


def test_thread_lifecycle_processes_jobs():
    h = Harness(answers={"user_engaged": 0.9})
    h.worker.start()
    try:
        h.worker.note("s1", "tamer_poke", h.now[0], dict(h.snapshot))
        deadline = h.now[0] + 2.0
        while not h.emitted and h.now[0] < deadline:
            time.sleep(0.02)
            h.now[0] += 0.02
        assert len(h.emitted) == 1
        assert h.emitted[0][1:3] == ("engage_user", "look")
        thread = h.worker._thread
        assert thread.is_alive()
    finally:
        h.worker.stop()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_thread_survives_unexpected_error():
    h = Harness(answers={"user_engaged": 0.9})
    calls = []
    real_emit = h.worker._emit_fn

    def flaky(sid, goal, action, payload):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real_emit(sid, goal, action, payload)

    h.worker._emit_fn = flaky
    h.worker.start()
    try:
        h.worker.note("s1", "tamer_poke", h.now[0], dict(h.snapshot))
        end = h.now[0] + 8.0
        while not h.emitted and h.now[0] < end:
            time.sleep(0.02)
            h.now[0] += 0.02
        assert h.worker._thread.is_alive()
        assert h.telemetry[0].error_type == "RuntimeError"
        h.worker.note("s1", "tamer_poke", h.now[0], dict(h.snapshot))
        end = h.now[0] + 8.0
        while not h.emitted and h.now[0] < end:
            time.sleep(0.02)
            h.now[0] += 0.02
        assert len(h.emitted) == 1
    finally:
        h.worker.stop()


def test_telemetry_row_carries_judge_metadata():
    h = Harness(answers={"user_engaged": 0.8})
    h.process()
    row = h.telemetry[0]
    assert row.questions_asked == 4
    assert row.trigger_event == "restlessness"
    assert row.lane == 2
    assert row.coalesced_kinds == ["restlessness"]
    assert "explore" in row.active_goals
    assert row.snapshot["soul_id"] == "s1"
    assert row.raw_judgments["user_engaged"] == 0.8
    assert row.latency_ms is not None
    assert row.queue_wait_ms is not None


class _TrackingCtx:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return object()

    def __exit__(self, *exc):
        self.exited += 1
        return False


def test_sensor_init_failure_closes_fresh_ctx(monkeypatch):
    import goapauto

    ctxs = []
    monkeypatch.setattr(
        goapauto,
        "shared_client",
        lambda timeout: ctxs.append(_TrackingCtx()) or ctxs[-1],
    )
    monkeypatch.setattr(
        jev,
        "SensorRegistry",
        lambda client: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    worker = JevWorker(
        queue=JevEventQueue(),
        breaker=JevBreaker(),
        emit_fn=lambda *a: None,
        telemetry_fn=lambda *a: None,
        suppressed_fn=lambda *a: False,
    )
    sensors, ctx = worker._make_sensors(None)
    assert sensors is None
    assert ctx is None
    assert len(ctxs) == 1
    assert ctxs[0].entered == 1
    assert ctxs[0].exited == 1


def test_sensor_init_retry_closes_each_ctx(monkeypatch):
    import goapauto

    ctxs = []
    monkeypatch.setattr(
        goapauto,
        "shared_client",
        lambda timeout: ctxs.append(_TrackingCtx()) or ctxs[-1],
    )
    monkeypatch.setattr(
        jev,
        "SensorRegistry",
        lambda client: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    worker = JevWorker(
        queue=JevEventQueue(),
        breaker=JevBreaker(),
        emit_fn=lambda *a: None,
        telemetry_fn=lambda *a: None,
        suppressed_fn=lambda *a: False,
    )
    for _ in range(3):
        sensors, ctx = worker._make_sensors(None)
        assert sensors is None
        assert ctx is None
    assert len(ctxs) == 3
    assert all(c.entered == 1 and c.exited == 1 for c in ctxs)


def test_sensor_init_recovers_after_failure(monkeypatch):
    import goapauto

    ctxs = []
    monkeypatch.setattr(
        goapauto,
        "shared_client",
        lambda timeout: ctxs.append(_TrackingCtx()) or ctxs[-1],
    )
    calls = {"n": 0}

    def registry(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return object()

    monkeypatch.setattr(jev, "SensorRegistry", registry)
    worker = JevWorker(
        queue=JevEventQueue(),
        breaker=JevBreaker(),
        emit_fn=lambda *a: None,
        telemetry_fn=lambda *a: None,
        suppressed_fn=lambda *a: False,
    )
    sensors, ctx = worker._make_sensors(None)
    assert sensors is None
    assert ctx is None
    assert ctxs[0].exited == 1
    sensors, ctx = worker._make_sensors(None)
    assert sensors is not None
    assert ctx is ctxs[1]
    assert ctxs[1].entered == 1
    assert ctxs[1].exited == 0


def test_rest_goal_emits_rest_action(monkeypatch):
    from ..agents import jev_brain

    h = Harness()
    monkeypatch.setattr(jev_brain, "arbitrate", lambda *a: jev_brain.GOAL_REST)
    h.process()
    assert len(h.emitted) == 1
    sid, goal, action, payload = h.emitted[0]
    assert (sid, goal, action) == ("s1", "rest", "rest")
    assert payload == {}
    assert h.telemetry[0].goal_selected == "rest"
