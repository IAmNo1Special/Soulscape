"""SimProcess entry point (issue #37).

The authoritative simulation as its own OS process. Owns the world
tick loop and the exclusive write discipline over sim-owned tables
(see sim_commands.py for the table inventory). Serves the localhost
IPC protocol (sim_ipc.SimServer) so the API process can submit
intents, run commands, and query tick state.

Run: ``python -m server.sim_process`` (or ``uv run python -m
server.sim_process``) from the repo root.

Config (env):
  SIM_HOST / SIM_PORT   IPC bind (default 127.0.0.1:9786)
  HUB_SECRET_KEY        required by nothing here; the sim trusts only
                        localhost callers (same-machine API process).
  SIM_TICK_HZ           tick rate override (default 5)

Lifecycle: init_db -> boot recovery (#16) -> boot intent pump ->
start tick -> serve IPC. SIGTERM/SIGINT or the ``shutdown`` IPC
message stops the tick (final flush + snapshot), then exits.
The world survives API restarts because the sim never stops ticking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading

from shared.env import load_app_env

load_app_env()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soulscape_hub")

from . import persistence  # noqa: E402
from .database import init_db  # noqa: E402
from .key_vault import install_redaction_filter  # noqa: E402
from .sim_ipc import (  # noqa: E402
    SIM_HOST,
    SIM_PORT,
    SimDispatcher,
    SimServer,
)
from .world_tick import TICK_HZ, WorldTick  # noqa: E402

_stop_event = threading.Event()


def _install_signal_handlers() -> None:
    def _request_stop(signum, _frame):
        # Set the event only: _amain()'s watch loop notices within
        # 0.5 s and shuts down cleanly. Stopping the loop itself here
        # would race asyncio.run()'s teardown.
        logger.info("sim received signal %s; stopping", signum)
        _stop_event.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


async def _amain() -> int:
    install_redaction_filter()
    logger.info("sim: initializing database...")
    init_db()

    tick_hz = float(os.getenv("SIM_TICK_HZ", str(TICK_HZ)))
    tick = WorldTick(tick_dt=1.0 / tick_hz)
    dispatcher = SimDispatcher(tick=tick)

    report = await asyncio.to_thread(persistence.recover_world, tick)
    logger.info(
        "sim boot recovery: mode=%s regime=%s souls=%d journal_replayed=%d "
        "gap=%.1fs tick_id=%d",
        report["mode"],
        report["regime"],
        report["souls"],
        report["journal_replayed"],
        report["gap_seconds"],
        report["tick_id"],
    )
    recovered = await asyncio.to_thread(tick.pump_intents)
    if recovered:
        logger.info("sim boot: adjudicated %d pending intents", recovered)
    from .agents import memory as _memory

    maint = await asyncio.to_thread(_memory.tick_maintenance)
    if maint.get("ran"):
        logger.info("sim boot: memory summarizer ran: %s", maint.get("report"))

    server = SimServer(dispatcher, host=SIM_HOST, port=SIM_PORT)
    dispatcher._on_shutdown = _stop_event.set

    _install_signal_handlers()

    logger.info("sim: starting world tick at %g Hz", tick_hz)
    await tick.start()
    server.start()
    logger.info("sim: up (tick_id=%d)", tick.tick_id)

    while not _stop_event.is_set():
        await asyncio.sleep(0.5)

    logger.info("sim: stopping...")
    server.stop()
    await tick.stop()
    logger.info("sim: stopped cleanly at tick_id=%d", tick.tick_id)
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
