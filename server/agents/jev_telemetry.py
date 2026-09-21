import json
from dataclasses import dataclass, field


@dataclass
class JevUsageRow:
    soul_id: str
    at: float
    trigger_event: str
    lane: int
    latency_ms: float | None = None
    queue_wait_ms: float | None = None
    input_tokens: int | None = None
    questions_asked: int = 4
    snapshot: dict = field(default_factory=dict)
    raw_judgments: dict = field(default_factory=dict)
    error_type: str | None = None
    stale_cache_hit: bool = False
    shed_count: int = 0
    coalesced_kinds: list = field(default_factory=list)
    active_goals: list = field(default_factory=list)
    goal_selected: str | None = None
    plan_action_count: int = 0


def record_usage(conn, row: JevUsageRow) -> None:
    conn.execute(
        "INSERT INTO jev_usage (soul_id, at, trigger_event, lane, latency_ms,"
        " queue_wait_ms, input_tokens, questions_asked, snapshot,"
        " raw_judgments, error_type, stale_cache_hit, shed_count,"
        " coalesced_event_kinds, active_goal_set, goal_selected,"
        " plan_action_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.soul_id,
            row.at,
            row.trigger_event,
            row.lane,
            row.latency_ms,
            row.queue_wait_ms,
            row.input_tokens,
            row.questions_asked,
            json.dumps(row.snapshot),
            json.dumps(row.raw_judgments),
            row.error_type,
            1 if row.stale_cache_hit else 0,
            row.shed_count,
            json.dumps(row.coalesced_kinds),
            json.dumps(row.active_goals),
            row.goal_selected,
            row.plan_action_count,
        ),
    )
