"""API-side gateway to the sim (issue #37).

The API process is read-mostly: domain reads go direct to SQLite
(short-lived connections; WAL readers see a consistent snapshot),
and every sim-table write crosses this gateway. Two backends share
the exact same message surface:

- IpcBackend: TCP to the SimProcess (production, docker compose).
- InProcessBackend: a SimHost in this process (unit tests; same
  dispatcher, same message dicts, no sockets).

Read-path decision (documented per the issue):
  Direct SQLite reads: every list/get endpoint (souls, marketplace,
    social, plots, mailbag, metering, presence, tamers, keys,
    bridge tokens, recaps). Fresh under WAL, never torn.
  IPC ``query`` reads: ``tick_status`` (/debug/tick -- the tick lives
    in the sim, the API has no copy) and ``positions`` (read-through
    soul positions+velocity: the sim's unflushed dirty set overlaid
    on the DB; the API process no longer shares that memory).

Write behavior: every mutating call raises SimUnreachable when the
sim is down. Routers map that to HTTP 503 and NEVER silently drop
an intent: ``submit_intent`` only returns after the sim's commit, so
no ack == not accepted == safe to retry with the same nonce
(idempotent via the (session_id, nonce) unique key).
"""

from __future__ import annotations

import os
import time
from typing import Any

from .sim_ipc import (
    SimClient,
    SimCommandError,
    SimError,
    SimHost,
    SimRefusal,
    SimUnreachable,
)

SETTLE_TIMEOUT_S = float(os.getenv("SIM_SETTLE_TIMEOUT_S", "15"))
SETTLE_POLL_S = 0.1


class IpcBackend:
    """Production backend: talks to the SimProcess over TCP."""

    def __init__(self, client: SimClient | None = None) -> None:
        self.client = client or SimClient()

    def handle(
        self, msg: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        return self.client.call(msg, timeout=timeout)

    def close(self) -> None:
        self.client.close()


class InProcessBackend:
    """Test backend: same dispatcher, same messages, no sockets."""

    def __init__(self, host: SimHost | None = None) -> None:
        self.host = host or SimHost()

    def handle(
        self, msg: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        return self.host.handle(msg)

    def pump(self) -> int:
        """Adjudicate pending intents (test helper)."""
        return self.host.pump()

    def close(self) -> None:
        pass


class SimGateway:
    """The API process's only path to sim-owned state."""

    def __init__(self, backend: IpcBackend | InProcessBackend) -> None:
        self.backend = backend

    def _call(
        self, msg: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        try:
            return self.backend.handle(msg, timeout=timeout)
        except SimUnreachable:
            raise
        except SimError as exc:
            raise SimUnreachable(str(exc)) from exc

    def ping(self) -> dict[str, Any]:
        started = time.monotonic()
        resp = self._call({"type": "ping"}, timeout=5.0)
        resp["latency_ms"] = (time.monotonic() - started) * 1000.0
        return resp

    def submit_intent(
        self,
        session_id: str,
        nonce: str,
        custodian_id: str | None,
        soul_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Durable enqueue. Returns the intent record.

        Raises SimRefusal on business-logic refusal, SimUnreachable
        when the sim is down (intent NOT accepted -- retry safely).
        """
        resp = self._call(
            {
                "type": "intent_submit",
                "session_id": session_id,
                "nonce": nonce,
                "custodian_id": custodian_id,
                "soul_id": soul_id,
                "kind": kind,
                "payload": payload,
            }
        )
        if resp.get("ok"):
            return resp["record"]
        if "refusal" in resp:
            refusal = resp["refusal"] or {}
            raise SimRefusal(
                refusal.get("reason", "internal"),
                refusal.get("detail", "internal"),
                ws_code=refusal.get("ws_code"),
            )
        error = resp.get("error") or {}
        raise SimError(error.get("detail") or "intent_submit failed")

    def intent_status(self, session_id: str, nonce: str) -> dict[str, Any] | None:
        resp = self._call(
            {"type": "intent_status", "session_id": session_id, "nonce": nonce}
        )
        if not resp.get("ok"):
            raise SimUnreachable("intent_status failed")
        return resp["record"]

    def await_settled(
        self,
        session_id: str,
        nonce: str,
        timeout: float = SETTLE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Poll until the sim adjudicates the intent.

        Returns the fresh record. On timeout returns the last-seen
        record (still pending): the intent is durable, the caller
        reports it as pending rather than failing. Raises
        SimUnreachable if the sim goes away mid-wait.
        """
        # In-process (test) backend: no tick loop is running, so
        # adjudicate once up front -- the same dispatcher code, just
        # pumped synchronously. Production ticks adjudicate on their
        # own; the poll loop below then observes the outcome.
        pump = getattr(self.backend, "pump", None)
        if callable(pump):
            pump()
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while True:
            record = self.intent_status(session_id, nonce)
            if record is not None:
                last = record
                if record.get("status") != "pending":
                    return record
            if time.monotonic() >= deadline:
                if last is None:
                    raise SimUnreachable("sim lost during settle wait")
                return last
            time.sleep(SETTLE_POLL_S)

    def command(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a sim command. Returns the result dict.

        Raises SimCommandError when the sim reports ok=false, and
        SimUnreachable when the sim is down.
        """
        resp = self._call({"type": "command", "name": name, "params": params})
        if resp.get("ok"):
            return resp["result"]
        error = resp.get("error") or {}
        raise SimCommandError(
            error.get("reason", "internal"), error.get("detail", "internal")
        )

    def tick_status(self) -> dict[str, Any]:
        resp = self._call({"type": "query", "name": "tick_status", "params": {}})
        if not resp.get("ok"):
            raise SimUnreachable("tick_status failed")
        return resp["result"]

    def positions(self) -> dict[str, dict[str, list[float]]]:
        """Read-through soul positions+velocity from the sim."""
        resp = self._call({"type": "query", "name": "positions", "params": {}})
        if not resp.get("ok"):
            raise SimUnreachable("positions query failed")
        return resp["result"]

    def shutdown(self) -> None:
        self._call({"type": "shutdown"})

    def close(self) -> None:
        self.backend.close()


_gateway: SimGateway | None = None


def get_gateway() -> SimGateway:
    """Process-wide gateway singleton, configured from the environment.

    SOULSCAPE_SIM_MODE=inprocess -> InProcessBackend (tests).
    Anything else -> IpcBackend talking to SIM_HOST:SIM_PORT.
    """
    global _gateway
    if _gateway is None:
        mode = os.getenv("SOULSCAPE_SIM_MODE", "ipc").lower()
        if mode == "inprocess":
            _gateway = SimGateway(InProcessBackend())
        else:
            _gateway = SimGateway(IpcBackend())
    return _gateway


def reset_gateway() -> None:
    """Drop the singleton (tests)."""
    global _gateway
    if _gateway is not None:
        _gateway.close()
    _gateway = None


def gateway_for(request: Any) -> SimGateway:
    """Fetch the gateway stashed on the FastAPI app state."""
    gw = getattr(request.app.state, "sim_gateway", None)
    if gw is None:
        gw = get_gateway()
    return gw
