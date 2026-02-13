from unittest.mock import AsyncMock, MagicMock

import pytest

from soulscape.core.soul.soul import Soul


@pytest.mark.asyncio
async def test_agent_context_generation(
    mock_network_service, mock_grimorium, mocker
):
    """
    Verifies that the Agent correctly captures state/sensations and sends them to the Runner.
    """
    soul = Soul(
        orb_color_rgb=(1.0, 0.0, 0.0),
        aura_color_rgb=(0.0, 1.0, 0.0),
        name="Context Test Soul",
        owner_id="test_owner",
        local_instance_id="test_owner",
    )

    # Mock Runner
    mock_runner = AsyncMock()
    soul.agent.runner = mock_runner

    async def mock_run_async(*args, **kwargs):
        """An empty async generator that does nothing."""
        if False:
            yield

    mock_runner.run_async.side_effect = mock_run_async

    snapshot = soul.create_snapshot()
    sensations = ["Hungry", "Cold"]

    await soul.agent._run_turn_async(
        name="Context Test Soul",
        state=snapshot,
        sensations=sensations,
        screen_context=None,
    )

    # Assert Runner called
    mock_runner.run_async.assert_called_once()
    call_args = mock_runner.run_async.call_args
    assert call_args is not None

    # Check arguments
    kwargs = call_args.kwargs
    new_message = kwargs.get("new_message")
    assert new_message is not None
    text_content = new_message.parts[0].text

    # Verify content
    assert "Current Location" in text_content
    assert "Hungry" in text_content
    assert "Cold" in text_content


@pytest.mark.asyncio
async def test_tool_wiring(mock_network_service, mock_grimorium, mocker):
    """
    Verifies that _initialize_magetools correctly binds tools to the Soul instance.
    """
    soul = Soul(
        orb_color_rgb=(1.0, 0.0, 0.0),
        aura_color_rgb=(0.0, 1.0, 0.0),
        name="Wiring Test Soul",
    )  # Mock the Grimorium instance inside the agent
    # The agent was initialized with a mock Grimorium class (from conftest)
    # So soul.agent._grimorium is a Mock object.

    mock_grim = soul.agent._grimorium

    # Setup registry with a fake tool
    mock_tool = MagicMock()
    mock_grim.registry = {"fake_spell": mock_tool}
    mock_grim.spell_sync = MagicMock()
    mock_grim.spell_sync.registry = {}

    # Manually trigger initialization
    # (It might have already run in background loop, but likely failed or didn't run due to mocks)
    # We force it here.
    soul.agent._magetools_initialized = False  # Reset flag
    await soul.agent._initialize_magetools()

    # Assert method bound to soul
    assert hasattr(soul, "fake_spell")

    # Execute bound method
    bound_method = getattr(soul, "fake_spell")
    bound_method(1, 2, 3)

    # Verify it called the original tool function with the soul as self
    mock_tool.assert_called_with(soul, 1, 2, 3)
