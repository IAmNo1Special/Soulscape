import threading

import pytest
from typesafe_sdk import Noul

from goapauto import JevSensor
from goapauto.testing import FakeTypeSafeClient

from ..agents import jev


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("SOULSCAPE_JEV_TIER", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    yield


def test_kill_switch_defaults_off():
    assert jev.enabled() is False


def test_kill_switch_on_values(monkeypatch):
    for val in ("1", "true", "on", "yes", "TRUE"):
        monkeypatch.setenv("SOULSCAPE_JEV_TIER", val)
        assert jev.enabled() is True


def test_kill_switch_off_values(monkeypatch):
    for val in ("0", "false", "off", ""):
        monkeypatch.setenv("SOULSCAPE_JEV_TIER", val)
        assert jev.enabled() is False


def test_api_key_present(monkeypatch):
    assert jev.api_key_present() is False
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert jev.api_key_present() is True


def test_build_questions_four_nouls():
    questions = jev.build_questions()
    assert set(questions) == {
        "threatened",
        "needs_pressing",
        "user_engaged",
        "should_rest",
    }
    for q in questions.values():
        assert isinstance(q, Noul)
        assert q.type == "noul"
        assert q.instructions


def test_sensor_judge_returns_scores():
    client = FakeTypeSafeClient(
        answers={
            "threatened": 0.1,
            "needs_pressing": 0.8,
            "user_engaged": 0.0,
            "should_rest": 0.4,
        }
    )
    sensor = jev.build_sensor(client)
    assert isinstance(sensor, JevSensor)
    out = sensor.judge({"satiety": 40.0})
    assert out == {
        "threatened": 0.1,
        "needs_pressing": 0.8,
        "user_engaged": 0.0,
        "should_rest": 0.4,
    }


def test_sensor_registry_one_per_soul():
    client = FakeTypeSafeClient(
        answers={k: 0.0 for k in jev.QUESTIONS},
    )
    reg = jev.SensorRegistry(client)
    a1 = reg.get("s1")
    a2 = reg.get("s1")
    b = reg.get("s2")
    assert a1 is a2
    assert a1 is not b


def test_sensor_registry_prune():
    client = FakeTypeSafeClient(answers={k: 0.0 for k in jev.QUESTIONS})
    reg = jev.SensorRegistry(client)
    reg.get("s1")
    reg.get("s2")
    reg.prune({"s1"})
    assert "s1" in reg._sensors
    assert "s2" not in reg._sensors


def test_telemetry_callback_receives_record():
    records = []
    client = FakeTypeSafeClient(answers={k: 0.5 for k in jev.QUESTIONS})
    sensor = jev.build_sensor(client, telemetry=records.append)
    sensor.judge({"x": 1})
    assert len(records) == 1
    assert records[0].source == "sensor"
    assert records[0].error is None


def _breaker(now):
    return jev.JevBreaker(failures=3, open_s=60.0, now_fn=lambda: now[0])


def test_breaker_closed_admits():
    br = _breaker([100.0])
    assert br.state == "closed"
    assert br.admit() is True


def test_breaker_opens_after_consecutive_failures():
    now = [100.0]
    br = _breaker(now)
    br.on_failure()
    br.on_failure()
    assert br.state == "closed"
    br.on_failure()
    assert br.state == "open"
    assert br.admit() is False


def test_breaker_success_resets_count():
    now = [100.0]
    br = _breaker(now)
    br.on_failure()
    br.on_failure()
    br.on_success()
    br.on_failure()
    br.on_failure()
    assert br.state == "closed"


def test_breaker_probe_after_open_window():
    now = [100.0]
    br = _breaker(now)
    for _ in range(3):
        br.on_failure()
    assert br.admit() is False
    now[0] = 159.0
    assert br.admit() is False
    now[0] = 161.0
    assert br.admit() is True
    assert br.admit() is False
    br.on_success()
    assert br.state == "closed"
    assert br.admit() is True


def test_breaker_failed_probe_reopens():
    now = [100.0]
    br = _breaker(now)
    for _ in range(3):
        br.on_failure()
    now[0] = 200.0
    assert br.admit() is True
    br.on_failure()
    assert br.state == "open"
    assert br.admit() is False
    now[0] = 261.0
    assert br.admit() is True


def test_breaker_probe_serialized_under_threads():
    now = [100.0]
    br = _breaker(now)
    for _ in range(3):
        br.on_failure()
    now[0] = 500.0
    admitted: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        admitted.append(br.admit())

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert admitted.count(True) == 1


def test_move_payload_cannot_inject_speed():
    from ..agents.pool import validate_agent_payload

    canonical = validate_agent_payload(
        "move_to", {"x": 100.0, "y": 100.0, "speed": 99999.0, "velocity": [1, 2]}
    )
    assert canonical is not None
    assert "speed" not in canonical
    assert "velocity" not in canonical
    assert canonical == {"x": 100.0, "y": 100.0}
