import json

import pytest

from ..agents import jev_telemetry
from ..agents.jev_telemetry import JevUsageRow


@pytest.fixture
def clean_table(db_conn):
    db_conn.execute("DELETE FROM jev_usage")
    db_conn.commit()
    yield
    db_conn.execute("DELETE FROM jev_usage")
    db_conn.commit()


def _row(**over):
    base = dict(
        soul_id="s1",
        at=1700000000.0,
        trigger_event="restlessness",
        lane=2,
        latency_ms=266.0,
        queue_wait_ms=120.0,
        input_tokens=859,
        questions_asked=4,
        snapshot={"satiety": 80.0},
        raw_judgments={"threatened": 0.1, "needs_pressing": 0.2},
        error_type=None,
        stale_cache_hit=False,
        shed_count=0,
        coalesced_kinds=["restlessness"],
        active_goals=["explore"],
        goal_selected="explore",
        plan_action_count=1,
    )
    base.update(over)
    return JevUsageRow(**base)


def test_record_and_read_back(db_conn, clean_table):
    jev_telemetry.record_usage(db_conn, _row())
    db_conn.commit()
    got = db_conn.execute("SELECT * FROM jev_usage").fetchone()
    assert got["soul_id"] == "s1"
    assert got["at"] == 1700000000.0
    assert got["trigger_event"] == "restlessness"
    assert got["lane"] == 2
    assert got["latency_ms"] == 266.0
    assert got["queue_wait_ms"] == 120.0
    assert got["input_tokens"] == 859
    assert got["questions_asked"] == 4
    assert json.loads(got["snapshot"]) == {"satiety": 80.0}
    assert json.loads(got["raw_judgments"])["needs_pressing"] == 0.2
    assert got["error_type"] is None
    assert got["stale_cache_hit"] == 0
    assert got["shed_count"] == 0
    assert json.loads(got["coalesced_event_kinds"]) == ["restlessness"]
    assert json.loads(got["active_goal_set"]) == ["explore"]
    assert got["goal_selected"] == "explore"
    assert got["plan_action_count"] == 1


def test_error_row_nullable_fields(db_conn, clean_table):
    jev_telemetry.record_usage(
        db_conn,
        _row(error_type="Timeout", goal_selected=None, plan_action_count=0),
    )
    db_conn.commit()
    got = db_conn.execute("SELECT * FROM jev_usage").fetchone()
    assert got["error_type"] == "Timeout"
    assert got["goal_selected"] is None
    assert got["plan_action_count"] == 0


def test_stale_cache_hit_stored(db_conn, clean_table):
    jev_telemetry.record_usage(db_conn, _row(stale_cache_hit=True))
    db_conn.commit()
    got = db_conn.execute("SELECT * FROM jev_usage").fetchone()
    assert got["stale_cache_hit"] == 1
