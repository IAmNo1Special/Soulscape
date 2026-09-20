"""Deterministic replay CLI (issue #38).

Forensic re-adjudication for dispute investigations::

    python -m server.replay --snapshot <id|path> --journal-tail <range> \\
        --seed S [--diff-against recorded] [--json] [--db PATH]

What it does:

1. Loads a snapshot (v2: full world-state dump) from the live DB's
   ``snapshots`` table (by id) or from a snapshot JSON file (by path).
2. Reads the journal tail ``<range>`` (``lo-hi`` | ``last:N`` |
   ``since:SEQ``; default: everything after the snapshot) from the
   live DB -- read-only, never written.
3. Rebuilds the exact pre-window world state in a **temporary**
   SQLite DB (snapshot dump + post-snapshot rows overlaid from live).
4. Re-runs each window intent through the **real in-process
   adjudication dispatch** (``WorldTick._adjudicate_one`` -- the same
   function the tick pump calls), in journal order, with the
   scenario-seeded RNGs and the per-intent frozen clock from
   ``server/determinism.py``.
5. Diffs recorded vs replayed outcomes per intent (status + result
   fields) and reports operator interventions from the window.

Exit 0: replay identical. Exit 1: divergent (or any usage failure).

The live DB is opened read-only (``mode=ro``); before/after sha256 +
mtime are reported as untouched evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import zlib

from . import determinism
from .persistence import ADJUDICATION_OUTCOME_TYPES

#: Outcome event types, ordered for the SQL IN clause (issue #38).
_OUTCOME_TYPES = tuple(sorted(ADJUDICATION_OUTCOME_TYPES))

#: Outcome event types that mean "rejected"; the rest mean adjudicated.
_REJECTED_TYPES = ("intent_rejected",)

#: Tables overlaid from the live DB after the snapshot restore, keyed
#: by primary key: rows born after the snapshot (new souls, listings,
#: intents...) that the window's intents may reference.
OVERLAY_TABLES = {
    "souls": "soul_id",
    "soul_inventory": "inventory_id",
    "intents": "intent_id",
    "escrows": "escrow_id",
    "marketplace": "listing_id",
    "messages": "message_id",
    "ledger": "ledger_id",
    "plots": "plot_id",
    "metering_events": "event_id",
    "decision_traces": "trace_id",
    "bridge_events": "event_id",
}


def canonical(value) -> str:
    """Canonical JSON for stable hashing/comparison (issue #38)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_snapshot_blob(raw: bytes) -> dict:
    """Decode a snapshot blob: zlib-compressed or raw JSON."""
    if raw[:2] == b"x\x9c" or raw[:2] == b"x\xda":
        raw = zlib.decompress(raw)
    snap = json.loads(raw.decode("utf-8"))
    if snap.get("v") != 2 or not isinstance(snap.get("tables"), dict):
        raise SystemExit(
            "error: snapshot is v1 (kinematics only) -- take a fresh "
            "snapshot with this build; v1 cannot rebuild replay state."
        )
    return snap


def open_live_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def journal_head(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(seq) AS head FROM journal").fetchone()
    return int(row["head"] or 0)


def parse_tail(spec: str | None, snap_seq: int, head: int) -> tuple[int, int]:
    """Parse --journal-tail into (lo, hi) inclusive seq bounds."""
    if spec is None:
        return (snap_seq + 1, head)
    spec = spec.strip()
    if spec.startswith("last:"):
        n = int(spec[5:])
        if n < 1:
            raise SystemExit("error: --journal-tail last:N needs N >= 1")
        return (max(1, head - n + 1), head)
    if spec.startswith("since:"):
        lo = int(spec[6:])
        return (lo, head)
    lo_s, _, hi_s = spec.partition("-")
    lo, hi = int(lo_s), int(hi_s)
    if lo < 1 or hi < lo:
        raise SystemExit(f"error: bad --journal-tail range: {spec!r}")
    if hi > head:
        raise SystemExit(
            f"error: --journal-tail hi ({hi}) is past journal head ({head})"
        )
    return (lo, hi)


def order_ids(adj_events: list[dict]) -> list[str]:
    """Intent ids in journal order (first outcome event wins)."""
    seen: list[str] = []
    for event in adj_events:
        intent_id = json.loads(event["payload"]).get("intent_id")
        if intent_id and intent_id not in seen:
            seen.append(intent_id)
    return seen


def read_window_events(
    conn: sqlite3.Connection, lo: int, hi: int
) -> tuple[list[dict], list[dict]]:
    """Window adjudication events (journal order) + operator events.

    Covers every outcome event type (issue #38): the generic
    intent_adjudicated/intent_rejected plus subsystem domain events
    (soul_fed, metering_debit_settled, tool.event), all carrying
    intent_id.
    """
    placeholders = ", ".join("?" for _ in _OUTCOME_TYPES)
    adj = [
        dict(row)
        for row in conn.execute(
            "SELECT seq, tick_id, type, payload, created_at FROM journal "
            f"WHERE seq BETWEEN ? AND ? AND type IN ({placeholders}) "
            "ORDER BY seq ASC",
            (lo, hi, *_OUTCOME_TYPES),
        ).fetchall()
    ]
    ops = [
        dict(row)
        for row in conn.execute(
            "SELECT seq, tick_id, type, payload, created_at FROM journal "
            "WHERE seq BETWEEN ? AND ? AND type LIKE 'operator.%' "
            "ORDER BY seq ASC",
            (lo, hi),
        ).fetchall()
    ]
    return adj, ops


def field_diffs(path: str, recorded, replayed) -> list[dict]:
    """Recursive field-level diff between two JSON values."""
    diffs: list[dict] = []
    if type(recorded) is not type(replayed):
        diffs.append(
            {"field": path or "(root)", "recorded": recorded, "replayed": replayed}
        )
    elif isinstance(recorded, dict):
        for key in sorted(set(recorded) | set(replayed)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in recorded:
                diffs.append({"field": sub, "recorded": None, "replayed": replayed[key]})
            elif key not in replayed:
                diffs.append({"field": sub, "recorded": recorded[key], "replayed": None})
            else:
                diffs.extend(field_diffs(sub, recorded[key], replayed[key]))
    elif isinstance(recorded, list):
        if len(recorded) != len(replayed):
            diffs.append(
                {
                    "field": path or "(root)",
                    "recorded": f"<list len {len(recorded)}>",
                    "replayed": f"<list len {len(replayed)}>",
                }
            )
        else:
            for i, (a, b) in enumerate(zip(recorded, replayed)):
                diffs.extend(field_diffs(f"{path}[{i}]", a, b))
    elif recorded != replayed:
        diffs.append(
            {"field": path or "(root)", "recorded": recorded, "replayed": replayed}
        )
    return diffs


def diff_recorded(intent_id: str, rec: dict, rep: dict) -> list[dict]:
    """Field-level diffs between the recorded and replayed outcome."""
    diffs = []
    if rec["journal_status"] != rec["status"]:
        # The journal and the intent row disagree about what was
        # recorded -- flag the inconsistency itself.
        diffs.append(
            {
                "field": "recorded_status_source",
                "recorded": (
                    f"journal={rec['journal_status']} "
                    f"intent_row={rec['status']}"
                ),
                "replayed": rep["status"],
            }
        )
    if rec["status"] != rep["status"]:
        diffs.append(
            {
                "field": "status",
                "recorded": rec["status"],
                "replayed": rep["status"],
            }
        )
    diffs.extend(field_diffs("result", rec["result"], rep["result"]))
    return diffs


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministically replay a journal window through "
        "the real adjudication code and diff against recorded outcomes."
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        help="Snapshot id in the live DB, or path to a snapshot JSON file.",
    )
    parser.add_argument(
        "--journal-tail",
        default=None,
        help="Journal seq range: 'lo-hi', 'last:N', 'since:SEQ'. "
        "Default: everything after the snapshot.",
    )
    parser.add_argument(
        "--seed",
        default=None,
        help="Scenario seed override. Default: the snapshot header's seed.",
    )
    parser.add_argument(
        "--diff-against",
        default="recorded",
        choices=["recorded"],
        help="Diff the replay against the recorded journal outcomes.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Live DB path. Default: SOULSCAPE_DB_PATH or the sim default.",
    )
    return parser


def resolve_db_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    # Import-free resolution: mirror database.DB_PATH's default.
    env = os.environ.get("SOULSCAPE_DB_PATH")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "soulscape_hub.db")


def run(args: argparse.Namespace) -> dict:
    """Execute the replay; returns the report dict (also printed)."""
    db_path = resolve_db_path(args.db)
    if not os.path.exists(db_path):
        raise SystemExit(f"error: live DB not found: {db_path}")

    # -- Live-DB untouched evidence: hash + mtime before anything. --
    live_before = {"sha256": sha256_file(db_path), "mtime": os.path.getmtime(db_path)}

    live = open_live_ro(db_path)
    tmp_path = None
    db_mod = None
    prev_db_env = os.environ.get("SOULSCAPE_DB_PATH")
    try:
        # -- Snapshot. --
        if args.snapshot.isdigit():
            row = live.execute(
                "SELECT blob FROM snapshots WHERE snapshot_id = ?",
                (int(args.snapshot),),
            ).fetchone()
            if row is None:
                raise SystemExit(
                    f"error: snapshot {args.snapshot} not found in live DB"
                )
            snapshot = load_snapshot_blob(bytes(row["blob"]))
            snapshot_id: int | str = int(args.snapshot)
        else:
            if not os.path.exists(args.snapshot):
                raise SystemExit(f"error: snapshot file not found: {args.snapshot}")
            with open(args.snapshot, "rb") as fh:
                snapshot = load_snapshot_blob(fh.read())
            snapshot_id = args.snapshot

        seed = args.seed or snapshot.get("scenario_seed")
        if not seed:
            row = live.execute(
                "SELECT payload FROM journal WHERE type = 'run_start' "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                try:
                    seed = json.loads(row["payload"]).get("seed")
                except (json.JSONDecodeError, TypeError, ValueError):
                    seed = None
        if not seed:
            seed = determinism.current_seed()
        if not seed:
            raise SystemExit(
                "error: no scenario seed: pass --seed or use a snapshot "
                "taken under SOULSCAPE_SCENARIO_SEED"
            )
        snap_derivation = snapshot.get("derivation")
        if (
            snap_derivation
            and snap_derivation != determinism.DERIVATION_VERSION
        ):
            print(
                f"warning: snapshot derivation {snap_derivation!r} != "
                f"running {determinism.DERIVATION_VERSION!r}; seeded RNG "
                "draws may diverge",
                file=sys.stderr,
            )

        head = journal_head(live)
        snap_seq = int(snapshot["journal_seq"])
        lo, hi = parse_tail(args.journal_tail, snap_seq, head)
        if lo > head:
            raise SystemExit(
                f"error: journal tail starts at {lo} but head is {head} "
                "(nothing recorded after the snapshot)"
            )

        # Outcome events from just after the snapshot: intents before
        # ``lo`` are a warm-up prefix -- replayed for state, not
        # diffed -- so an explicit sub-range still starts from the
        # true pre-window world state.
        adj_events, op_events = read_window_events(live, snap_seq + 1, hi)
        window_events = [e for e in adj_events if e["seq"] >= lo]
        if not window_events:
            raise SystemExit(
                f"error: no intent adjudications in journal seq {lo}-{hi}"
            )

        # Recorded outcomes, in journal order. The journal gives the
        # order, tick_id, and frozen clock; the intent row is the
        # authoritative outcome -- journal payloads may redact result
        # fields (market strips "item") or omit them entirely.
        placeholders = ",".join("?" for _ in order_ids(adj_events))
        intent_rows = {
            row["intent_id"]: dict(row)
            for row in live.execute(
                f"SELECT * FROM intents WHERE intent_id IN ({placeholders})",
                order_ids(adj_events),
            ).fetchall()
        }
        recorded: dict[str, dict] = {}
        order: list[str] = []
        for event in adj_events:
            payload = json.loads(event["payload"])
            intent_id = payload.get("intent_id")
            if not intent_id:
                continue
            if intent_id in recorded:
                # A subsystem emitting two outcome rows for one intent:
                # the first (lowest seq) is the outcome.
                continue
            row = intent_rows.get(intent_id)
            if row is None:
                raise SystemExit(
                    f"error: window intent {intent_id} missing from "
                    "live intents table"
                )
            journal_status = (
                "rejected"
                if event["type"] in _REJECTED_TYPES
                else "adjudicated"
            )
            raw_result = row["result"]
            recorded[intent_id] = {
                "seq": event["seq"],
                "tick_id": event["tick_id"],
                "journal_status": journal_status,
                "status": row["status"],
                "kind": row["kind"] or payload.get("kind"),
                "soul_id": row["soul_id"] or payload.get("soul_id"),
                "result": json.loads(raw_result) if raw_result else None,
                "created_at": event["created_at"],
            }
            order.append(intent_id)

        # Warm-up prefix (seq < lo): replayed for state, never diffed.
        window_ids = [i for i in order if recorded[i]["seq"] >= lo]

        interventions = [
            {
                "seq": event["seq"],
                "tick_id": event["tick_id"],
                "type": event["type"],
                **json.loads(event["payload"]),
                "created_at": event["created_at"],
            }
            for event in op_events
        ]

        # -- Build the temp DB: snapshot state + post-snapshot overlay. --
        tmp = tempfile.NamedTemporaryFile(
            prefix="soulscape-replay-", suffix=".db", delete=False
        )
        tmp_path = tmp.name
        tmp.close()

        os.environ["SOULSCAPE_DB_PATH"] = tmp_path
        # Imports must come after the env override so database.DB_PATH
        # points at the temp file. Set DB_PATH explicitly too: in a
        # long-lived process server.database may already be imported.
        from server import affection  # noqa: E402
        from server import database  # noqa: E402
        from server import intents as intents_module  # noqa: E402
        from server import persistence  # noqa: E402
        from server import world_tick  # noqa: E402

        db_mod = database
        database.DB_PATH = tmp_path

        database.init_db()
        with database.get_db() as conn:
            persistence.restore_world_tables(conn, snapshot["tables"])
            live_tables = {
                table: [dict(r) for r in live.execute(f"SELECT * FROM {table}")]
                for table in OVERLAY_TABLES
                if table
                in {
                    r["name"]
                    for r in live.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            }
            for table, pk in OVERLAY_TABLES.items():
                for row in live_tables.get(table, []):
                    cols = ", ".join(row.keys())
                    holders = ", ".join("?" for _ in row)
                    conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({cols}) "
                        f"VALUES ({holders})",
                        tuple(row.values()),
                    )
            # Reset the window to its pre-adjudication inputs: intents
            # pending with no result, escrows held.
            for intent_id in order:
                conn.execute(
                    "UPDATE intents SET status = 'pending', result = NULL "
                    "WHERE intent_id = ?",
                    (intent_id,),
                )
                conn.execute(
                    "UPDATE escrows SET status = 'held' "
                    "WHERE intent_id = ? AND status IN ('applied', 'released')",
                    (intent_id,),
                )
            # Rows the window's own adjudications created must not be
            # overlaid from live (the snapshot restore + INSERT OR
            # IGNORE never overwrote snapshot rows, so only
            # post-snapshot creations by window intents need removal):
            # replay re-creates them through the real code.
            for intent_id in order:
                result = recorded[intent_id]["result"] or {}
                for key, table, column in (
                    ("message_id", "messages", "message_id"),
                    ("listing_id", "marketplace", "listing_id"),
                    ("event_id", "bridge_events", "event_id"),
                ):
                    value = result.get(key)
                    if value:
                        conn.execute(
                            f"DELETE FROM {table} WHERE {column} = ?", (value,)
                        )
                # Metering settlement writes per-event ledger rows under
                # "{intent_id}:evt:{event_id}".
                conn.execute(
                    "DELETE FROM ledger WHERE intent_id = ? "
                    "OR intent_id LIKE ?",
                    (intent_id, f"{intent_id}:evt:%"),
                )
            # Metering settlement mutates (not creates) metering_events:
            # restore the unsettled-but-batched state the settlement
            # replays from. batch_id stays: the operator batch action
            # assigned it before adjudication.
            batch_ids = {
                row["batch_id"]
                for row in conn.execute(
                    "SELECT json_extract(payload, '$.batch_id') AS batch_id "
                    "FROM intents WHERE kind = 'metering_debit' "
                    f"AND intent_id IN ({','.join('?' for _ in order)})",
                    order,
                ).fetchall()
                if row["batch_id"]
            }
            for batch_id in batch_ids:
                conn.execute(
                    "UPDATE metering_events SET essence_charged = NULL, "
                    "shortfall_essence = 0.0, settled_at = NULL, "
                    "settled_by = NULL WHERE batch_id = ?",
                    (batch_id,),
                )
            conn.commit()

        # -- Replay: real adjudication, journal order, frozen clock. --
        persistence.dirty.clear()
        affection.reset_carry_state()
        tick = world_tick.WorldTick(scenario_seed=seed)
        tick.replay = determinism.ReplayContext(seed, recorded)

        replayed: dict[str, dict] = {}
        with database.get_db() as conn:
            for intent_id in order:
                row = conn.execute(
                    "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()
                intent = intents_module._row_to_dict(row)
                tick.tick_id = int(recorded[intent_id]["tick_id"] or 0)
                tick.replay.begin_intent(intent_id)
                tick._adjudicate_one(intent)
                back = conn.execute(
                    "SELECT status, result FROM intents WHERE intent_id = ?",
                    (intent_id,),
                ).fetchone()
                raw_result = back["result"]
                replayed[intent_id] = {
                    "status": back["status"],
                    "result": json.loads(raw_result) if raw_result else None,
                }

        # -- Diff. --
        intents_out = []
        divergent = 0
        for intent_id in window_ids:
            rec, rep = recorded[intent_id], replayed[intent_id]
            diffs = diff_recorded(intent_id, rec, rep)
            if diffs:
                divergent += 1
            intents_out.append(
                {
                    "intent_id": intent_id,
                    "seq": rec["seq"],
                    "kind": rec["kind"],
                    "soul_id": rec["soul_id"],
                    "recorded_status": rec["status"],
                    "replayed_status": rep["status"],
                    "recorded_result": rec["result"],
                    "replayed_result": rep["result"],
                    "identical": not diffs,
                    "diffs": diffs,
                }
            )

        # Warm-up prefix divergence: the window replay cannot be
        # trusted when the state it was rebuilt from already differs.
        prefix_divergent = [
            intent_id
            for intent_id in order
            if intent_id not in window_ids
            and diff_recorded(intent_id, recorded[intent_id], replayed[intent_id])
        ]

        outcome_hash = hashlib.sha256(
            canonical(
                [
                    {
                        "intent_id": i["intent_id"],
                        "status": i["replayed_status"],
                        "result": i["replayed_result"],
                    }
                    for i in intents_out
                ]
            ).encode("utf-8")
        ).hexdigest()

        live_after = {
            "sha256": sha256_file(db_path),
            "mtime": os.path.getmtime(db_path),
        }

        report = {
            "snapshot": {"id": snapshot_id, "tick_id": snapshot["tick_id"]},
            "seed": seed,
            "journal_tail": {"lo": lo, "hi": hi},
            "n_intents": len(window_ids),
            "n_warmup": len(order) - len(window_ids),
            "n_divergent": divergent,
            "prefix_divergent": prefix_divergent,
            "identical": divergent == 0 and not prefix_divergent,
            "outcome_hash": outcome_hash,
            "intents": intents_out,
            "interventions": interventions,
            "live_db": {
                "path": db_path,
                "sha256_before": live_before["sha256"],
                "sha256_after": live_after["sha256"],
                "mtime_before": live_before["mtime"],
                "mtime_after": live_after["mtime"],
                "untouched": (
                    live_before["sha256"] == live_after["sha256"]
                    and live_before["mtime"] == live_after["mtime"]
                ),
            },
        }
        return report
    finally:
        live.close()
        if db_mod is not None:
            db_mod.DB_PATH = db_path
        if prev_db_env is None:
            os.environ.pop("SOULSCAPE_DB_PATH", None)
        else:
            os.environ["SOULSCAPE_DB_PATH"] = prev_db_env
        if tmp_path:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp_path + suffix)
                except OSError:
                    pass


def print_human(report: dict) -> None:
    snap = report["snapshot"]
    tail = report["journal_tail"]
    print(
        f"soulscape replay -- snapshot {snap['id']} (tick {snap['tick_id']}), "
        f"seed {report['seed']!r}, journal {tail['lo']}-{tail['hi']}"
    )
    print(
        f"{report['n_intents']} intents replayed, "
        f"{report['n_divergent']} divergent, "
        f"outcome hash {report['outcome_hash'][:16]}"
    )
    if report.get("n_warmup"):
        print(f"({report['n_warmup']} warm-up intents replayed for state, not diffed)")
    if report.get("prefix_divergent"):
        print(
            "WARNING: warm-up prefix diverged; window replay untrusted: "
            + ", ".join(report["prefix_divergent"])
        )
    print()
    for item in report["intents"]:
        mark = "OK " if item["identical"] else "DIFF"
        print(
            f"[{mark}] {item['intent_id']} {item['kind']} "
            f"recorded={item['recorded_status']} "
            f"replayed={item['replayed_status']}"
        )
        for diff in item["diffs"]:
            print(
                f"      {diff['field']}: "
                f"recorded={canonical(diff['recorded'])} "
                f"replayed={canonical(diff['replayed'])}"
            )
    if report["interventions"]:
        print()
        print(f"operator interventions in window ({len(report['interventions'])}):")
        for op in report["interventions"]:
            print(
                f"  seq {op['seq']} {op['type']} "
                f"by={op.get('operator_id')} "
                f"target={op.get('target_type')}:{op.get('target_id')} "
                f"{op.get('details', '')}"
            )
    print()
    live = report["live_db"]
    state = "untouched" if live["untouched"] else "MODIFIED"
    print(f"live DB {state} (sha256 {live['sha256_after'][:16]})")
    print("VERDICT: " + ("IDENTICAL" if report["identical"] else "DIVERGENT"))


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report = run(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- CLI: report, don't traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(canonical(report))
    else:
        print_human(report)
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
