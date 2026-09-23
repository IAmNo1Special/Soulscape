from __future__ import annotations

import pytest

from ..core.soul.soul import Soul

# Ensure imports work (handled by pyproject.toml now)


@pytest.fixture
def mock_logger(mocker):
    """Mock the logger to prevent console spam during tests."""
    return mocker.patch("client.system.logger.log")


@pytest.fixture
def mock_network_service(mocker):
    """Mock the NetworkService to prevent network calls."""
    mock = mocker.patch(
        "client.system.network_service.NetworkService", autospec=True
    )
    return mock.return_value


@pytest.fixture
def mock_gui_service(mocker):
    """Mock the GuiService to prevent GUI process spawning."""
    mock = mocker.patch("client.ui.gui.gui_service.GuiService", autospec=True)
    return mock.return_value


@pytest.fixture(autouse=True)
def disable_baseline_wander():
    """Deterministic Hub stepping for e2e tests (see
    server/tests/conftest.py). The client e2e suites spin a real
    in-process tick; an unseeded wander would move idle souls out
    from under render assertions."""
    from server import wander as wander_mod

    was = wander_mod.ENABLED
    wander_mod.ENABLED = False
    yield
    wander_mod.ENABLED = was


@pytest.fixture(autouse=True)
def auto_cleanup_souls(monkeypatch):
    """Automatically tracks and cleans up Soul instances created during tests."""
    created_souls = []

    # Capture the original unbound __init__
    original_init = Soul.__init__

    def wrapped_init(self, *args, **kwargs):
        created_souls.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Soul, "__init__", wrapped_init)

    yield

    # Teardown: Stop all tracked souls
    for soul in created_souls:
        try:
            if hasattr(soul, "stop"):
                soul.stop()
        except Exception as e:
            # It's useful to see errors during cleanup, even if we don't re-raise.
            print(f"WARNING: Error during auto-cleanup of soul: {e}")
