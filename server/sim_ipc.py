"""Sim IPC: localhost protocol between the API process and SimProcess.

Issue #37 extracted the authoritative simulation into its own OS process.
The API process never writes sim tables; every mutation crosses this
protocol and is executed by the sim, which holds the exclusive write
discipline over sim-owned tables.

Transport: TCP on 127.0.0.1 (``SIM_HOST``/``SIM_PORT`` env, defaults
``127.0.0.1``/``9786``). TCP was chosen over a Unix socket so the two
processes can later move to separate containers without protocol churn
(docs/architecture.md); the frames are identical either way. msgpack
comes later -- JSON for now.

Framing: 4-byte big-endian unsigned length prefix + UTF-8 JSON body.
Max frame 16 MiB. One request frame -> exactly one response frame per
connection; connections are short-lived (one call each) or
thread-pinned by the gateway.

Message table (every message carries ``type``):

  ping
    -> {"ok": true, "tick_id": int, "tick_running": bool,
        "server_time": float}
    Liveness probe. The API /health endpoint reports this latency.

  intent_submit
    {"type": "intent_submit", "session_id": str, "nonce": str,
     "custodian_id": str | None, "soul_id": str, "kind": str,
     "payload": dict}
    -> {"ok": true, "record": {...}, "created": bool}
    -> {"ok": false, "refusal": {"reason": str, "detail": str}}
    Durable enqueue executed by the sim. The commit lands BEFORE the
    response is written, so an ``ok`` ack means the intent row is
    durable (#14 commit-before-ack, preserved across the split). The
    sim dispatches ``kind`` to the kind-specific enqueue function
    (market/social/plot/feed/presence escrow paths); unknown kinds fall
    back to the generic enqueue. Business refusals come back as
    ``refusal`` -- never raised.

  intent_status
    {"type": "intent_status", "session_id": str, "nonce": str}
    -> {"ok": true, "record": {...} | None}
    Read-only poll used by the API to await tick adjudication.

  query
    {"type": "query", "name": str, "params": dict}
    -> {"ok": true, "result": ...}
    Parameterized reads only -- no raw SQL crosses the wire. Allowed
    names: ``tick_status`` (the old /debug/tick snapshot),
    ``positions`` (read-through soul positions+velocity, i.e. the
    sim's unflushed dirty set overlaid on the DB).
    All other API reads go direct to SQLite (short-lived connections;
    WAL readers see a consistent snapshot, never torn state).

  command
    {"type": "command", "name": str, "params": dict}
    -> {"ok": true, "result": ...}
    -> {"ok": false, "error": {"reason": str, "detail": str}}
    The small set of sim mutations routers need. Executed under the
    tick's step lock, in the sim process, with the same transactions
    the routers used to run inline. Names (see sim_commands.py):
    ``souls_upsert``, ``quip_reserve``, ``quip_release``,
    ``quip_charge``, ``plot_set_policy``, ``mailbag_answer``,
    ``metering_set_pricing``, ``metering_settle``,
    ``bridge_ingest``, ``bridge_commentary_finalize``.

  shutdown
    -> {"ok": true}
    Asks the sim to stop its tick loop, final-flush, snapshot, and
    exit. Operator-gated on the API side.

Errors: unknown ``type`` -> ``{"ok": false, "error":
{"reason": "unknown_message_type", ...}}``. Malformed frames close the
connection without a response.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger("soulscape_hub")

SIM_HOST = os.getenv("SIM_HOST", "127.0.0.1")
SIM_PORT = int(os.getenv("SIM_PORT", "9786"))
FRAME_HEADER = struct.Struct(">I")
MAX_FRAME_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT_S = 2.0
CALL_TIMEOUT_S = 15.0

_MSG_PING = "ping"
_MSG_INTENT_SUBMIT = "intent_submit"
_MSG_INTENT_STATUS = "intent_status"
_MSG_QUERY = "query"
_MSG_COMMAND = "command"
_MSG_SHUTDOWN = "shutdown"


class SimError(Exception):
    """Base for sim IPC failures."""


class SimUnreachable(SimError):
    """The sim process could not be reached (down, refusing, timeout).

    Callers map this to HTTP 503. An intent that never got an ack was
    never accepted -- the client may safely retry with the same nonce.
    """


class SimRefusal(SimError):
    """Business-logic refusal from the sim (enqueue-time validation).

    Carries ``reason``/``detail`` like the domain ``*Refusal``
    exceptions so router ``_refusal_http`` mappers keep working
    unchanged. ``ws_code`` is populated when the sim's domain refusal
    defines one (market/plots/biology).
    """

    def __init__(
        self, reason: str, detail: str = "", ws_code: str | None = None
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.ws_code = ws_code


class SimCommandError(SimError):
    """A ``command`` message returned ok=false."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def send_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise SimError("frame too large")
    sock.sendall(FRAME_HEADER.pack(len(body)) + body)


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    chunks = []
    remaining = n
    while remaining:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise SimUnreachable("sim read timeout")
        sock.settimeout(timeout)
        chunk = sock.recv(remaining)
        if not chunk:
            raise SimUnreachable("sim closed connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    header = _recv_exact(sock, FRAME_HEADER.size, deadline)
    (length,) = FRAME_HEADER.unpack(header)
    if length > MAX_FRAME_BYTES or length == 0:
        raise SimError("bad frame length")
    body = _recv_exact(sock, length, deadline)
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SimError(f"bad frame body: {exc}") from exc
    if not isinstance(obj, dict):
        raise SimError("frame is not an object")
    return obj


class SimDispatcher:
    """Executes IPC messages against sim-owned state.

    Owns the ``WorldTick``. Write messages (``intent_submit``,
    ``command``) run under the tick's step lock so tick work and
    ingress serialize on the sim's single disciplined write path.
    Read messages (``ping``, ``intent_status``, ``query``) never take
    the step lock except ``tick_status``, which snapshots in-memory
    counters.
    """

    def __init__(self, tick: Any | None = None) -> None:
        from . import world_tick as _wt

        self.tick = tick if tick is not None else _wt.WorldTick()
        self._on_shutdown: Callable[[], None] | None = None

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        try:
            if msg_type == _MSG_PING:
                return self._ping()
            if msg_type == _MSG_INTENT_SUBMIT:
                return self._intent_submit(msg)
            if msg_type == _MSG_INTENT_STATUS:
                return self._intent_status(msg)
            if msg_type == _MSG_QUERY:
                return self._query(msg)
            if msg_type == _MSG_COMMAND:
                return self._command(msg)
            if msg_type == _MSG_SHUTDOWN:
                return self._shutdown()
        except SimRefusal as exc:
            return {
                "ok": False,
                "refusal": {"reason": exc.reason, "detail": exc.detail},
            }
        except SimCommandError as exc:
            return {"ok": False, "error": {"reason": exc.reason, "detail": exc.detail}}
        except Exception as exc:  # never leak tracebacks over the wire
            logger.exception("sim dispatch failed for %s", msg_type)
            return {"ok": False, "error": {"reason": "internal", "detail": str(exc)}}
        return {
            "ok": False,
            "error": {
                "reason": "unknown_message_type",
                "detail": f"unknown type: {msg_type!r}",
            },
        }

    def _ping(self) -> dict[str, Any]:
        return {
            "ok": True,
            "tick_id": self.tick.tick_id,
            "tick_running": self.tick.running,
            "server_time": time.time(),
        }

    def _intent_submit(self, msg: dict[str, Any]) -> dict[str, Any]:
        from . import sim_commands

        with self.tick._step_lock:
            try:
                record, created = sim_commands.submit_intent(
                    session_id=msg["session_id"],
                    nonce=msg["nonce"],
                    custodian_id=msg.get("custodian_id"),
                    soul_id=msg["soul_id"],
                    kind=msg["kind"],
                    payload=msg.get("payload") or {},
                )
            except Exception as exc:
                reason = getattr(exc, "reason", None)
                if reason is None and isinstance(exc, ValueError):
                    reason = "bad_payload"
                if reason is not None:
                    detail = getattr(exc, "detail", None) or str(exc) or reason
                    return {
                        "ok": False,
                        "refusal": {
                            "reason": reason,
                            "detail": detail,
                            "ws_code": getattr(exc, "ws_code", None),
                        },
                    }
                raise
        return {"ok": True, "record": record, "created": created}

    def _intent_status(self, msg: dict[str, Any]) -> dict[str, Any]:
        from . import intents

        record = intents.get_intent_by_nonce(msg["session_id"], msg["nonce"])
        return {"ok": True, "record": record}

    def _query(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name")
        if name == "tick_status":
            with self.tick._step_lock:
                result = self.tick.snapshot()
            return {"ok": True, "result": result}
        if name == "positions":
            from . import database, persistence

            with self.tick._step_lock:
                with database.get_db() as conn:
                    state = persistence.read_state_through(conn)
            return {
                "ok": True,
                "result": {
                    sid: {"position": v["position"], "velocity": v["velocity"]}
                    for sid, v in state.items()
                },
            }
        return {
            "ok": False,
            "error": {"reason": "unknown_query", "detail": f"unknown query: {name!r}"},
        }

    def _command(self, msg: dict[str, Any]) -> dict[str, Any]:
        from . import sim_commands

        name = msg.get("name")
        params = msg.get("params") or {}
        fn = sim_commands.COMMANDS.get(name)
        if fn is None:
            return {
                "ok": False,
                "error": {
                    "reason": "unknown_command",
                    "detail": f"unknown command: {name!r}",
                },
            }
        with self.tick._step_lock:
            try:
                result = fn(params, self.tick)
            except SimCommandError:
                raise
            except Exception as exc:
                reason = getattr(exc, "reason", None) or "internal"
                detail = getattr(exc, "detail", None) or str(exc) or reason
                return {"ok": False, "error": {"reason": reason, "detail": detail}}
        return {"ok": True, "result": result}

    def _shutdown(self) -> dict[str, Any]:
        if self._on_shutdown is not None:
            self._on_shutdown()
        return {"ok": True}


class SimServer:
    """TCP server hosting a SimDispatcher on localhost.

    Bounded worker pool (no unbounded threads): each connection is
    handled by one pool worker, one request frame at a time.
    """

    def __init__(
        self,
        dispatcher: SimDispatcher,
        host: str = SIM_HOST,
        port: int = SIM_PORT,
        max_workers: int = 8,
    ) -> None:
        self.dispatcher = dispatcher
        self.host = host
        self.port = port
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="sim-ipc"
        )
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(32)
        sock.settimeout(0.5)
        self._sock = sock
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="sim-ipc-accept", daemon=True
        )
        self._accept_thread.start()
        logger.info("sim IPC listening on %s:%d", self.host, self.port)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stopping.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._pool.submit(self._handle_conn, conn)

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            with conn:
                while not self._stopping.is_set():
                    try:
                        msg = recv_frame(conn, CALL_TIMEOUT_S)
                    except (SimUnreachable, TimeoutError):
                        logger.info("sim connection idle, closing")
                        break
                    try:
                        response = self.dispatcher.handle(msg)
                    except Exception:
                        logger.exception("sim handler crashed")
                        break
                    try:
                        send_frame(conn, response)
                    except OSError:
                        break
        except Exception:
            logger.exception("sim connection handler failed")

    def stop(self) -> None:
        self._stopping.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._pool.shutdown(wait=True, cancel_futures=True)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5.0)


class SimClient:
    """API-side client: one request frame -> one response frame.

    Uses a thread-pinned persistent connection (threading.local) so
    concurrent request threads don't share a socket and reconnects
    stay bounded. Any transport failure raises SimUnreachable.
    """

    def __init__(
        self,
        host: str = SIM_HOST,
        port: int = SIM_PORT,
        timeout: float = CALL_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._local = threading.local()

    def _connect(self) -> socket.socket:
        sock = socket.create_connection(
            (self.host, self.port), timeout=CONNECT_TIMEOUT_S
        )
        return sock

    def _sock(self) -> socket.socket:
        sock = getattr(self._local, "sock", None)
        if sock is None:
            try:
                sock = self._connect()
            except OSError as exc:
                raise SimUnreachable(
                    f"sim unreachable at {self.host}:{self.port}: {exc}"
                ) from exc
            self._local.sock = sock
        return sock

    def _drop(self) -> None:
        sock = getattr(self._local, "sock", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            self._local.sock = None

    def call(self, msg: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        timeout = self.timeout if timeout is None else timeout
        try:
            return self._round_trip(msg, timeout)
        except (OSError, SimError):
            self._drop()
            return self._round_trip(msg, timeout)

    def _round_trip(self, msg: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            send_frame(self._sock(), msg)
        except (OSError, SimError) as exc:
            self._drop()
            raise SimUnreachable(f"sim send failed: {exc}") from exc
        try:
            return recv_frame(self._sock(), timeout)
        except (OSError, SimError) as exc:
            self._drop()
            if isinstance(exc, SimUnreachable):
                raise
            raise SimUnreachable(f"sim recv failed: {exc}") from exc

    def close(self) -> None:
        self._drop()


class SimHost:
    """In-process sim for the unit test suite: the same dispatcher,
    the same message dicts, no sockets. Lets TestClient-style tests
    run the full API<->sim split inside one process."""

    def __init__(self, tick: Any | None = None) -> None:
        self.dispatcher = SimDispatcher(tick=tick)

    @property
    def tick(self) -> Any:
        return self.dispatcher.tick

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self.dispatcher.handle(msg)

    def pump(self) -> int:
        """Adjudicate pending intents synchronously (test helper)."""
        return self.tick.pump_intents()
