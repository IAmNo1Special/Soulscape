import asyncio
from typing import Any

from magetools import spell

from soulscape.utils.helpers import action_guard


@action_guard
@spell
async def wait_x_secs(self, seconds: float) -> dict[str, Any]:
    """Pauses agent execution for a specified duration.

    Args:
        seconds: The number of seconds to wait.

    Returns:
        A success message after the wait completes.
    """
    await asyncio.sleep(seconds)
    return {
        "status": "success",
        "message": f"Waited for {seconds} seconds.",
    }


@spell
async def cancel_action(self, action_name: str | None = None) -> dict[str, Any]:
    """Cancels specific or all ongoing actions/movement.

    Args:
        action_name: The name of the specific tool to cancel (e.g. 'move_to').
                        If omitted, all actions and movement are halted.

    Returns:
        A success message.
    """
    if action_name == "move_to":
        self.physics.target_location = None
        msg = "Movement cancelled."
    elif action_name:
        self._active_actions.discard(action_name)
        msg = f"Action '{action_name}' cancelled."
    else:
        self.physics.target_location = None
        self._active_actions.clear()
        msg = "All actions cancelled."

    return {"status": "success", "message": msg}
