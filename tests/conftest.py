import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# Ensure the src directory is in the path for imports
sys.path.insert(0, "./src")


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_logger(mocker):
    """Mock the logger to prevent console spam during tests."""
    return mocker.patch("soulscape.system.logger.log")


@pytest.fixture
def mock_network_service(mocker):
    """Mock the NetworkService to prevent network calls."""
    mock = mocker.patch(
        "soulscape.system.network_service.NetworkService", autospec=True
    )
    return mock.return_value


@pytest.fixture
def mock_gui_service(mocker):
    """Mock the GuiService to prevent GUI process spawning."""
    mock = mocker.patch(
        "soulscape.ui.gui.gui_service.GuiService", autospec=True
    )
    return mock.return_value


@pytest.fixture
def mock_grimorium(mocker):
    """Mock Grimorium interaction."""
    return mocker.patch("soulscape.core.soul.agent.Grimorium", autospec=True)


@pytest.fixture
def mock_vision(mocker):
    """Mock pyautogui to prevent screenshotting."""
    return mocker.patch("pyautogui.screenshot")
