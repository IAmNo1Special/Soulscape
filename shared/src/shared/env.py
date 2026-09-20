"""One .env loading contract for every Soulscape entry point."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_app_env() -> bool:
    """Load ``.env`` from the current working directory.

    Exported environment variables always win over ``.env`` values.
    Every process — Hub server, SimProcess, client, agent — resolves
    secrets identically, so a stale export can never silently desync
    one process from the others.
    """
    return load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
