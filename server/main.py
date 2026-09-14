import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .database import init_db
from .routers import marketplace, social, souls, websockets

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize the database
    logger.info("Initializing database...")
    init_db()

    if not os.getenv("HUB_SECRET_KEY"):
        logger.warning(
            "⚠️  HUB_SECRET_KEY is not set in .env! Authentication will fail."
        )

    yield
    # Shutdown: Clean up resources if needed
    logger.info("Shutting down...")


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


# Include Routers
app.include_router(marketplace.router)
app.include_router(social.router)
app.include_router(souls.router)
app.include_router(websockets.router)


def main():
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", os.getenv("PORT", "9785")))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
