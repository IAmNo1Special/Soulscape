import math
from typing import Any

from magetools import spell

from soulscape.core.commands import MoveCommand
from soulscape.system.logger import log
from soulscape.utils.helpers import action_guard


@action_guard
@spell
async def look_around(self) -> dict[str, Any]:
    """Utilizes the soul's sensory organs to perceive other nearby entities.

    Scans the immediate vicinity for other souls. Provides relative
    directions and distances to any detected presence within its vision
    radius.

    Returns:
        A dictionary containing status, message, and a list of visible souls.
    """
    nearby_souls_info = []

    # Calculate vision radius (must match _run_agent_step logic)
    vision_stat: float = 0.0
    if self.biology.stats:
        vision_stat = float(self.biology.stats.vision)

    # Scale radius: Use a moderate base radius so souls can see nearby but not too far.
    # 150px is a good "awareness" zone on screen (300px diameter).
    vision_radius: int = max(100, int(vision_stat * 1.5))

    for other in self.soul_registry:
        if other.biology.soul_id == self.biology.soul_id:
            continue

        dx = other.x - self.x
        dy = other.y - self.y
        dist = math.sqrt(dx * dx + dy * dy)

        # Skip souls outside of vision range
        if dist > vision_radius:
            continue

        # Simple relative description
        dir_x = "East" if dx > 0 else "West"
        dir_y = "South" if dy > 0 else "North"

        nearby_souls_info.append(
            {
                "name": other.biology.name,
                "distance": round(dist, 1),
                "direction": f"{abs(dx):.1f}px {dir_x}, {abs(dy):.1f}px {dir_y}",
                "status": "Alive" if other.biology.is_alive() else "Perished",
            }
        )

    if not nearby_souls_info:
        return {
            "status": "success",
            "message": "You look around but see no other souls nearby.",
            "data": {"souls": []},
        }

    summary = "You see other souls: " + ", ".join(
        [
            f"{s['name']} is {s['direction']} away ({s['status']})"
            for s in nearby_souls_info
        ]
    )
    return {
        "status": "success",
        "message": summary,
        "data": {"souls": nearby_souls_info},
    }


@action_guard
@spell
async def move_to(self, x: int, y: int) -> dict[str, Any]:
    """Initiates a long-running travel to specific screen coordinates.

    The soul will use its physics engine to navigate to (x, y) over several
    seconds. This tool returns once movement has STARTED. The framework
    will pause your turn until arrival. Do NOT call this tool repeatedly
    for the same destination unless you want to change course.

    Args:
        x: Target absolute x-coordinate on the screen.
        y: Target absolute y-coordinate on the screen.

    Returns:
        A dictionary acknowledging the start of the journey.
    """
    # Clamp to screen dimensions
    sw = self.physics.screen_width
    sh = self.physics.screen_height

    x = max(0, min(x, sw))
    y = max(0, min(y, sh))

    log.info(f"{self.biology.name} is moving to ({x}, {y})")

    # Queue the move instead of direct mutation
    self.command_queue.put(MoveCommand(x=float(x), y=float(y)))

    return {
        "status": "started",
        "message": f"Journey started to ({x}, {y}).",
        "data": {
            "destination": (x, y),
            "current_pos": (self.x, self.y),
            "progress": 0.0,
        },
    }
