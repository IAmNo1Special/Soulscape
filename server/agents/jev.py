import os
import threading
import time

from typesafe_sdk import Noul

from goapauto import JevSensor

RESTLESS_AFTER_S = 30.0
SWEEP_EVERY_S = 15.0
RESTLESS_JITTER_S = 15.0
FLOOR_TAMER_S = 1.0
FLOOR_DEFAULT_S = 5.0
SHED_AFTER_S = 15.0
JUDGE_TIMEOUT_S = 8.0
JEV_AMBLE_SPEED = 175.0
SENSOR_MAX_STALE_S = 30.0
JEV_NOUL_THRESHOLD = 0.5
BREAKER_FAILURES = 3
BREAKER_OPEN_S = 60.0
WANDER_LEGS_MIN = 2
WANDER_LEGS_MAX = 4
WANDER_LEG_MIN_U = 80.0
WANDER_LEG_MAX_U = 150.0
WANDER_DWELL_MIN_S = 2.0
WANDER_DWELL_MAX_S = 5.0
WANDER_SPEED_MIN = 150.0
WANDER_SPEED_MAX = 200.0
MOVE_SPEED_MIN = 50.0

QUESTIONS = ("threatened", "needs_pressing", "user_engaged", "should_rest")

TAMER_KINDS = frozenset({"tamer_poke"})

LANE_TAMER = 0
LANE_NORMAL = 1
LANE_RESTLESS = 2

KIND_LANE = {
    "tamer_poke": LANE_TAMER,
    "tamer_presence": LANE_TAMER,
    "vision_enter": LANE_NORMAL,
    "vision_exit": LANE_NORMAL,
    "movement_complete": LANE_NORMAL,
    "wander_continue": LANE_NORMAL,
    "wallet_delta": LANE_NORMAL,
    "needs_change": LANE_NORMAL,
    "carry_change": LANE_NORMAL,
    "restlessness": LANE_RESTLESS,
}

KILL_SWITCH_ENV = "SOULSCAPE_JEV_TIER"
API_KEY_ENV = "TYPESAFE_API_KEY"


def enabled() -> bool:
    return os.getenv(KILL_SWITCH_ENV, "").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


def api_key_present() -> bool:
    return bool(os.getenv(API_KEY_ENV, "").strip())


def _question(instructions: str, true_means: str, false_means: str) -> Noul:
    return Noul(
        type="noul",
        instructions=instructions,
        criteria={"true": true_means, "false": false_means},
    )


def build_questions() -> dict[str, Noul]:
    return {
        "threatened": _question(
            "How threatened does the soul seem by its surroundings?",
            "something here is dangerous and the soul should get away",
            "the surroundings feel safe",
        ),
        "needs_pressing": _question(
            "How urgently do the soul's physical needs require action?",
            "hunger, thirst, or health need action soon",
            "physical needs are fine for now",
        ),
        "user_engaged": _question(
            "How actively is the tamer interacting with the soul right now?",
            "the tamer is actively poking, petting, or talking to the soul",
            "no active tamer interaction",
        ),
        "should_rest": _question(
            "Would resting be the right call for the soul right now?",
            "the soul seems tired and should settle",
            "the soul seems alert and active",
        ),
    }


def build_sensor(client, telemetry=None) -> JevSensor:
    return JevSensor(
        observe=lambda: {},
        questions=build_questions(),
        client=client,
        max_stale=SENSOR_MAX_STALE_S,
        telemetry=telemetry,
    )


class SensorRegistry:
    def __init__(self, client) -> None:
        self._client = client
        self._sensors: dict[str, JevSensor] = {}
        self._telemetry = None

    def bind_telemetry(self, telemetry) -> None:
        self._telemetry = telemetry

    def get(self, soul_id: str) -> JevSensor:
        sensor = self._sensors.get(soul_id)
        if sensor is None:
            sensor = build_sensor(self._client, telemetry=self._telemetry)
            self._sensors[soul_id] = sensor
        return sensor

    def prune(self, keep: set[str]) -> None:
        for soul_id in [s for s in self._sensors if s not in keep]:
            del self._sensors[soul_id]


class JevBreaker:
    def __init__(
        self,
        failures: int = BREAKER_FAILURES,
        open_s: float = BREAKER_OPEN_S,
        now_fn=time.time,
    ) -> None:
        self._failures = failures
        self._open_s = open_s
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._state = "closed"
        self._consecutive = 0
        self._opened_at = 0.0
        self._probe_out = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def admit(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if self._now_fn() - self._opened_at < self._open_s:
                    return False
                self._state = "half_open"
                self._probe_out = False
            if self._probe_out:
                return False
            self._probe_out = True
            return True

    def on_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consecutive = 0
            self._probe_out = False

    def on_failure(self) -> None:
        with self._lock:
            self._consecutive += 1
            if self._state == "half_open" or self._consecutive >= self._failures:
                self._state = "open"
                self._opened_at = self._now_fn()
                self._probe_out = False
