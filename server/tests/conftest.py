import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient  # noqa: E402

from .. import database  # noqa: E402
from .. import main  # noqa: E402
from ..rate_limit import limiter  # noqa: E402

load_dotenv()


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Isolate tests from the process-global sliding-window limiter."""
    limiter._hits.clear()
    yield
    limiter._hits.clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def setup_test_db(tmp_path_factory):
    """Sets up a temporary database for the entire test session."""
    temp_dir = tmp_path_factory.mktemp("data")
    test_db_path = str(temp_dir / "test_soulscape_hub.db")

    # Patch the DB_PATH in the database module
    database.DB_PATH = test_db_path

    # Re-initialize the DB for tests
    database.init_db()

    yield test_db_path

    # Clean up is handled by tmp_path_factory


@pytest.fixture
def db_conn():
    """Provides a connection to the test database with Row factory."""
    with database.get_db() as conn:
        yield conn


@pytest.fixture
def client():
    """Provides a FastAPI TestClient with authentication."""
    hub_secret = os.getenv("HUB_SECRET_KEY", "soulscape-secret-123")
    return TestClient(main.app, headers={"X-Hub-Secret": hub_secret})


@pytest.fixture(autouse=True)
def clear_db(db_conn):
    """Clears all tables before each test to ensure isolation."""
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM marketplace")
    cursor.execute("DELETE FROM messages")
    cursor.execute("DELETE FROM souls")
    cursor.execute("DELETE FROM soul_inventory")
    cursor.execute("DELETE FROM ws_sessions")
    cursor.execute("DELETE FROM tamer_sessions")
    cursor.execute("DELETE FROM tamers")
    cursor.execute("DELETE FROM ws_tickets")
    cursor.execute("DELETE FROM rate_limits")
    cursor.execute("DELETE FROM audit_log")
    cursor.execute("DELETE FROM intents")
    cursor.execute("DELETE FROM journal")
    cursor.execute("DELETE FROM snapshots")
    cursor.execute("DELETE FROM escrows")
    cursor.execute("DELETE FROM ledger")
    cursor.execute("DELETE FROM plots")
    cursor.execute("DELETE FROM llm_keys")
    cursor.execute("DELETE FROM llm_usage")
    cursor.execute("DELETE FROM metering_events")
    cursor.execute("DELETE FROM decision_traces")
    cursor.execute("DELETE FROM metering_config")
    cursor.execute("DELETE FROM episodes")
    cursor.execute("DELETE FROM weekly_digests")
    cursor.execute("DELETE FROM semantic_memories")
    # The journal is append-only in production, so recovery treats its seq
    # column as gapless. Reset the AUTOINCREMENT sequences too, or a test that
    # leaves journal/snapshot rows behind would hand the next test a journal
    # that starts mid-sequence and looks corrupt.
    cursor.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('journal', 'snapshots', 'episodes', 'weekly_digests', 'llm_usage')"
    )
    cursor.execute("UPDATE globals SET value = 0.0 WHERE key = 'essence_fund'")
    cursor.execute("UPDATE globals SET value = 0.0 WHERE key = 'plot_claim_seq'")
    cursor.execute(
        "DELETE FROM globals WHERE key = 'ledger_fund_baseline' "
        "OR key LIKE 'ledger_base:%' "
        "OR key = 'memory_last_summarize_at'"
    )
    db_conn.commit()
    from .. import plots

    plots.seed_plots(db_conn)
    db_conn.commit()
    from .. import persistence

    persistence.dirty.clear()
    from ..agents import memory as _memory

    _memory.reset_volatile()
    from ..agents import sensations as _sensations

    _sensations.clear()
    from ..agents import deliberation as _deliberation

    _deliberation.clear_identity_cache()
    from ..routers.marketplace import _marketplace_cache

    _marketplace_cache["timestamp"] = 0.0
    _marketplace_cache["data"] = None


@pytest.fixture
def register_soul(client):
    """Returns a function to quickly register a soul in the test DB."""

    def _register(
        soul_id: str,
        essence: float = 100.0,
        name: str = "Test Soul",
        secret: str | None = None,
    ):
        owner_id = f"owner_{soul_id}"
        payload = {
            "owner_id": owner_id,
            "souls": [
                {
                    "soul_id": soul_id,
                    "owner_id": owner_id,
                    "name": name,
                    "essence": essence,
                    "hp": 100,
                    "max_hp": 100,
                    "satiety": 100,
                    "hydration": 100,
                    "secret": secret,
                }
            ],
        }
        res = client.post("/souls", json=payload)
        assert res.status_code == 200
        # Essence is server-owned (POST /souls ignores client essence), so
        # fund test souls directly. Production code has no such path.
        if essence != 100.0:
            with database.get_db() as conn:
                conn.execute(
                    "UPDATE souls SET essence = ? WHERE soul_id = ?",
                    (essence, soul_id),
                )
                conn.commit()
        return res.json()

    return _register
