import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient  # noqa: E402

from .. import database  # noqa: E402
from .. import main  # noqa: E402

load_dotenv()


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
    cursor.execute("DELETE FROM social_posts")
    cursor.execute("DELETE FROM social_replies")
    cursor.execute("DELETE FROM souls")
    cursor.execute("DELETE FROM soul_inventory")
    cursor.execute("UPDATE globals SET value = 0.0 WHERE key = 'essence_fund'")
    db_conn.commit()


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
        return res.json()

    return _register
