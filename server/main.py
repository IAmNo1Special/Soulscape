import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import persistence
from .key_vault import install_redaction_filter
from .sim_gateway import gateway_for, get_gateway, reset_gateway
from .sim_ipc import SimUnreachable
from . import viewport
from .routers import (
    bridge,
    keys,
    mailbag,
    marketplace,
    metering,
    plots,
    presence,
    social,
    souls,
    tamers,
    websockets,
)
from .security import UserIdentity, get_api_key

_rate_limit_store: dict[str, list[float]] = {}
_RATE_LIMIT_RPS = int(os.getenv("RATE_LIMIT_RPS", "100"))
_RATE_LIMIT_ENABLED = os.getenv("DISABLE_RATE_LIMIT", "").lower() not in (
    "1",
    "true",
    "yes",
)


def _check_rate_limit(key: str) -> bool:
    now = time.time()
    window = now - 1.0
    _rate_limit_store.setdefault(key, [])
    _rate_limit_store[key] = [t for t in _rate_limit_store[key] if t > window]
    if len(_rate_limit_store[key]) >= _RATE_LIMIT_RPS:
        return False
    _rate_limit_store[key].append(now)
    return True


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soulscape_hub")
install_redaction_filter()


def _wire_positions_provider(gateway) -> None:
    """Feed the viewport pump read-through positions from the sim.

    The API process no longer shares the sim's in-memory dirty set,
    so the viewport overlays the sim's ``positions`` query instead.
    If the sim is unreachable, fall back to the DB view (stale by at
    most one flush interval, but always a consistent snapshot).
    """

    def _positions() -> dict[str, tuple[float, float]]:
        try:
            result = gateway.positions()
        except SimUnreachable:
            return persistence.read_positions_through()
        out: dict[str, tuple[float, float]] = {}
        for soul_id, entry in result.items():
            try:
                pos = entry["position"]
                out[soul_id] = (float(pos[0]), float(pos[1]))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        return out

    viewport.set_positions_provider(_positions)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Issue #37: the API process NEVER initializes or migrates the
    # database -- the SimProcess owns the schema and all sim-table
    # writes. The API only verifies the database is readable.
    from .database import DB_PATH, _get_connection

    if not os.path.exists(DB_PATH):
        logger.error("database %s missing -- start the SimProcess first", DB_PATH)
    else:
        try:
            conn = _get_connection()
            conn.execute("SELECT 1 FROM souls LIMIT 1")
            conn.close()
        except Exception as exc:
            logger.error("database %s not readable: %s", DB_PATH, exc)
    install_redaction_filter()

    if not os.getenv("HUB_SECRET_KEY"):
        logger.warning("HUB_SECRET_KEY is not set in .env! Authentication will fail.")

    gateway = get_gateway()
    app.state.sim_gateway = gateway
    _wire_positions_provider(gateway)
    try:
        ping = await asyncio.to_thread(gateway.ping)
        logger.info(
            "sim reachable: tick_id=%d tick_running=%s latency=%.1fms",
            ping["tick_id"],
            ping["tick_running"],
            ping["latency_ms"],
        )
    except SimUnreachable:
        logger.warning(
            "sim unreachable at startup -- intents will 503 until the "
            "SimProcess is up; reads serve from SQLite"
        )

    yield
    logger.info("Shutting down...")
    viewport.set_positions_provider(None)
    reset_gateway()


app = FastAPI(
    title="Soulscape Hub",
    lifespan=lifespan,
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if not _RATE_LIMIT_ENABLED:
        return await call_next(request)
    if request.url.path in ("/health", "/ws"):
        return await call_next(request)
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})
    return await call_next(request)


@app.get("/health", dependencies=[])
async def health(request: Request):
    try:
        from .database import _get_connection

        conn = _get_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    sim: dict = {"reachable": False}
    try:
        ping = await asyncio.to_thread(gateway_for(request).ping)
        sim = {
            "reachable": True,
            "latency_ms": round(ping["latency_ms"], 2),
            "tick_id": ping["tick_id"],
            "tick_running": ping["tick_running"],
        }
    except SimUnreachable:
        pass
    status = "online" if (db_ok and sim["reachable"]) else "degraded"
    return {"status": status, "db_ok": db_ok, "sim": sim}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.get("/debug/tick")
async def debug_tick(request: Request, identity: UserIdentity = Depends(get_api_key)):
    if not identity.is_operator:
        raise HTTPException(status_code=403, detail="Operator only")
    try:
        return await asyncio.to_thread(gateway_for(request).tick_status)
    except SimUnreachable:
        raise HTTPException(status_code=503, detail="Simulation unavailable")


app.include_router(bridge.router)
app.include_router(keys.router)
app.include_router(mailbag.router)
app.include_router(marketplace.router)
app.include_router(metering.router)
app.include_router(plots.router)
app.include_router(presence.router)
app.include_router(social.router)
app.include_router(souls.router)
app.include_router(tamers.router)
app.include_router(websockets.router)


def main():
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", os.getenv("PORT", "9785")))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
