import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .database import init_db
from . import persistence
from .key_vault import install_redaction_filter
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
from .world_tick import TICK_HZ, WorldTick, hub_authoritative_enabled

# Simple in-memory rate limiter
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


# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soulscape_hub")
install_redaction_filter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize the database
    logger.info("Initializing database...")
    init_db()
    install_redaction_filter()

    if not os.getenv("HUB_SECRET_KEY"):
        logger.warning(
            "⚠️  HUB_SECRET_KEY is not set in .env! Authentication will fail."
        )

    app.state.world_tick = WorldTick()
    if hub_authoritative_enabled():
        tick = app.state.world_tick
        report = await asyncio.to_thread(persistence.recover_world, tick)
        logger.info(
            "boot recovery: mode=%s regime=%s souls=%d journal_replayed=%d "
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
            logger.info("boot recovery: adjudicated %d pending intents", recovered)
        # #26: boot catch-up for the nightly memory summarizer (runs
        # only when >24h since the last run).
        from .agents import memory as _memory

        maint = await asyncio.to_thread(_memory.tick_maintenance)
        if maint.get("ran"):
            logger.info("boot: memory summarizer ran: %s", maint.get("report"))
        logger.info("hub_authoritative=1: starting world tick at %d Hz", TICK_HZ)
        await app.state.world_tick.start()
    else:
        logger.info("hub_authoritative flag off: world tick disabled")

    yield
    # Shutdown: Clean up resources if needed
    logger.info("Shutting down...")
    tick = getattr(app.state, "world_tick", None)
    if tick is not None:
        await tick.stop()


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
async def health():
    try:
        from .database import _get_connection

        conn = _get_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "online", "db_ok": db_ok}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.get("/debug/tick")
async def debug_tick(identity: UserIdentity = Depends(get_api_key)):
    if not identity.is_operator:
        raise HTTPException(status_code=403, detail="Operator only")
    tick = getattr(app.state, "world_tick", None)
    if tick is None:
        return WorldTick().snapshot()
    return tick.snapshot()


# Include Routers
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
