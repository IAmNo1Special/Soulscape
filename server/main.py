import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from .database import init_db
from .routers import marketplace, social, souls, websockets

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

# Include Routers
app.include_router(marketplace.router)
app.include_router(social.router)
app.include_router(souls.router)
app.include_router(websockets.router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
