"""Unit tests for the client presence pipeline (issue #28).

Covers the trust boundary: the redactor's only input channel is the
PresenceSampler interface; hostile raw signals (exact timestamps, window
titles with URLs, app paths, keystroke counts) fed through a fake sampler
must never appear in the emitted payload. Also covers the structural
assertion (the interface cannot even express those signals), event
transitions, the opt-in gate, and the change+heartbeat shipping cadence.
"""

from __future__ import annotations

import inspect
import json

import pytest

from client.system import presence as pm


HOSTILE_TITLE = "Banking - https://evil.example/login - C:\\Users\\x\\secrets"
HOSTILE_PATH = "C:\\Users\\x\\AppData\\Roaming\\evil\\steal.exe"


class HostileSampler(pm.FakePresenceSampler):
    """A sampler that returns raw hostile data where the interface allows.

    The interface only has three methods, so hostility is limited to
    values those methods can carry -- that IS the structural guarantee.
    """

    def __init__(self) -> None:
        super().__init__(age_s=3723.456, locked=False, category=HOSTILE_TITLE)


def test_sampler_interface_is_minimal_and_closed():
    methods = {
        name for name, _ in inspect.getmembers(pm.PresenceSampler, inspect.isfunction)
    }
    abstract = {
        name
        for name, m in inspect.getmembers(pm.PresenceSampler)
        if getattr(m, "__isabstractmethod__", False)
    }
    assert abstract == {
        "last_input_age_s",
        "is_locked",
        "foreground_category",
    }
    assert methods == abstract or methods <= abstract | {"__init__"}
    joined = " ".join(abstract).lower()
    for forbidden in (
        "keystroke",
        "content",
        "screenshot",
        "title",
        "path",
        "url",
        "mic",
        "camera",
        "keylog",
    ):
        assert forbidden not in joined


def test_redactor_constructor_takes_only_sampler_and_opt_in():
    sig = inspect.signature(pm.PresenceRedactor.__init__)
    params = [p for p in sig.parameters if p != "self"]
    assert params == ["sampler", "app_category_opt_in", "clock"]
    src = inspect.getsource(pm.PresenceRedactor)
    for forbidden in ("keystroke", "screenshot", "title", "path", "url", "mic"):
        assert forbidden not in src.lower()


def test_hostile_signals_never_leave_redactor():
    redactor = pm.PresenceRedactor(HostileSampler(), app_category_opt_in=False)
    payload = redactor.sample()
    assert payload == {"presence": "idle", "idle_bucket": "30+"}
    blob = json.dumps(payload)
    assert "3723" not in blob
    assert "evil.example" not in blob
    assert HOSTILE_TITLE not in blob
    assert "C:\\" not in blob
    assert "app_category" not in payload


def test_opt_in_off_drops_hostile_category():
    redactor = pm.PresenceRedactor(
        pm.FakePresenceSampler(age_s=10.0, category="game"),
        app_category_opt_in=False,
    )
    assert redactor.sample() == {"presence": "active", "idle_bucket": "0-5"}


def test_opt_in_on_emits_closed_category_only():
    redactor = pm.PresenceRedactor(
        pm.FakePresenceSampler(age_s=10.0, category="game"),
        app_category_opt_in=True,
    )
    payload = redactor.sample()
    assert payload["app_category"] == "game"
    assert payload["app_category"] in pm.APP_CATEGORIES


def test_opt_in_on_still_redacts_hostile_category():
    redactor = pm.PresenceRedactor(HostileSampler(), app_category_opt_in=True)
    payload = redactor.sample()
    assert "app_category" not in payload
    blob = json.dumps(payload)
    assert "evil.example" not in blob


def test_idle_bucket_boundaries():
    cases = [
        (0.0, "active", "0-5"),
        (299.9, "active", "0-5"),
        (300.0, "idle", "5-30"),
        (1799.9, "idle", "5-30"),
        (1800.0, "idle", "30+"),
        (99999.0, "idle", "30+"),
    ]
    for age, presence, bucket in cases:
        payload = pm.PresenceRedactor(pm.FakePresenceSampler(age_s=age)).sample()
        assert payload["presence"] == presence, age
        assert payload["idle_bucket"] == bucket, age


def test_lock_unlock_events():
    clock = [1000.0]
    sampler = pm.FakePresenceSampler(age_s=5.0, locked=False)
    redactor = pm.PresenceRedactor(sampler, clock=lambda: clock[0])
    assert "event" not in redactor.sample()
    sampler.locked = True
    assert redactor.sample()["event"] == "lock"
    sampler.locked = True
    assert "event" not in redactor.sample()
    sampler.locked = False
    assert redactor.sample()["event"] == "unlock"
    assert "event" not in redactor.sample()


def test_tamer_return_after_long_absence():
    clock = [1000.0]
    sampler = pm.FakePresenceSampler(age_s=4000.0)
    redactor = pm.PresenceRedactor(sampler, clock=lambda: clock[0])
    assert "event" not in redactor.sample()
    clock[0] += pm.TAMER_RETURN_ABSENCE_SECONDS + 1
    sampler.age_s = 3.0
    payload = redactor.sample()
    assert payload["event"] == "tamer_return"
    assert payload["presence"] == "active"
    assert "event" not in redactor.sample()


def test_no_tamer_return_for_short_absence():
    clock = [1000.0]
    sampler = pm.FakePresenceSampler(age_s=4000.0)
    redactor = pm.PresenceRedactor(sampler, clock=lambda: clock[0])
    redactor.sample()
    clock[0] += 60.0
    sampler.age_s = 3.0
    assert "event" not in redactor.sample()


def test_windows_sampler_refuses_non_windows():
    import sys

    if sys.platform == "win32":
        pytest.skip("Windows-only negative test")
    with pytest.raises(RuntimeError):
        pm.WindowsPresenceSampler()


def test_build_sampler_never_breaks_import():
    sampler = pm.build_sampler()
    assert isinstance(sampler, pm.PresenceSampler)


def test_opt_in_settings_default_off(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert pm.get_app_category_opt_in() is False


def test_pipeline_ships_on_change_then_heartbeat():
    clock = [1000.0]
    sampler = pm.FakePresenceSampler(age_s=5.0)
    redactor = pm.PresenceRedactor(sampler, clock=lambda: clock[0])
    shipped: list[dict] = []

    def send(kind, soul_id, **fields):
        shipped.append({"kind": kind, "soul_id": soul_id, **fields})

    pipe = pm.PresencePipeline(
        redactor,
        send=send,
        get_soul_id=lambda: "soul_1",
        heartbeat_s=60.0,
        clock=lambda: clock[0],
    )
    first = pipe.tick_once()
    assert first is not None and shipped[0]["kind"] == "tamer_presence"
    assert shipped[0]["presence"] == "active"
    assert pipe.tick_once() is None
    assert len(shipped) == 1
    clock[0] += 30.0
    assert pipe.tick_once() is None
    clock[0] += 31.0
    assert pipe.tick_once() is not None
    assert len(shipped) == 2
    sampler.age_s = 400.0
    payload = pipe.tick_once()
    assert payload is not None
    assert payload["presence"] == "idle"
    assert shipped[-1]["idle_bucket"] == "5-30"
    assert len(shipped) == 3


def test_pipeline_payload_matches_server_allowlist():
    sampler = pm.FakePresenceSampler(age_s=5.0, category="browser")
    redactor = pm.PresenceRedactor(sampler, app_category_opt_in=True)
    shipped: list[dict] = []
    pipe = pm.PresencePipeline(
        redactor, send=lambda k, s, **f: shipped.append(f), get_soul_id=lambda: "x"
    )
    pipe.tick_once()
    payload = shipped[0]
    assert set(payload) <= {"presence", "idle_bucket", "event", "app_category"}
    assert payload["presence"] in ("active", "idle", "locked")
    assert payload["idle_bucket"] in ("0-5", "5-30", "30+")


def test_pipeline_defers_ship_until_session_ready():
    clock = [1000.0]
    sampler = pm.FakePresenceSampler(age_s=5.0)
    redactor = pm.PresenceRedactor(sampler, clock=lambda: clock[0])
    shipped: list[dict] = []
    ready = [False]
    pipe = pm.PresencePipeline(
        redactor,
        send=lambda k, s, **f: shipped.append({"kind": k, **f}),
        get_soul_id=lambda: "soul_1",
        heartbeat_s=60.0,
        clock=lambda: clock[0],
        ready=lambda: ready[0],
    )
    assert pipe.tick_once() is None
    assert pipe.tick_once() is None
    assert shipped == []
    ready[0] = True
    first = pipe.tick_once()
    assert first is not None
    assert len(shipped) == 1
    assert shipped[0]["kind"] == "tamer_presence"
    assert shipped[0]["presence"] == "active"


def test_pipeline_gate_preserves_edge_events():
    clock = [1000.0]
    sampler = pm.FakePresenceSampler(age_s=5.0, locked=False)
    redactor = pm.PresenceRedactor(sampler, clock=lambda: clock[0])
    shipped: list[dict] = []
    ready = [False]
    pipe = pm.PresencePipeline(
        redactor,
        send=lambda k, s, **f: shipped.append({"kind": k, **f}),
        get_soul_id=lambda: "soul_1",
        clock=lambda: clock[0],
        ready=lambda: ready[0],
    )
    sampler.locked = True
    assert pipe.tick_once() is None
    assert shipped == []
    ready[0] = True
    payload = pipe.tick_once()
    assert payload is not None
    assert shipped[-1]["presence"] == "locked"
    assert shipped[-1]["event"] == "lock"
