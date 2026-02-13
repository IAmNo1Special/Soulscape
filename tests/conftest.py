import pytest

# Ensure imports work (handled by pyproject.toml now)


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
