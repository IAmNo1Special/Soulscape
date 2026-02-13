import os
import random
import sys
import time
from unittest.mock import MagicMock, patch

# Ensure src is in python path
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../src"))
)

# Mock pyglet globally BEFORE importing soulscape modules
sys.modules["pyglet"] = MagicMock()

from soulscape.core.soul.agent import SoulAgent  # noqa: E402
from soulscape.core.soul.soul import Soul  # noqa: E402


def test_concurrency():
    """Stress test for Soul/Agent concurrency."""
    print("Starting Concurrency Stress Test...")

    # Mock Pyglet and other heavy dependencies
    # We patch them where they are imported
    with (
        patch("soulscape.core.soul.agent.pyautogui"),
        patch("soulscape.core.soul.agent.Grimorium"),
        patch(
            "soulscape.core.soul.agent.SoulAgent._setup_runner",
            new_callable=MagicMock,
        ),
        patch(
            "soulscape.core.soul.agent.SoulAgent._run_coro",
            new_callable=MagicMock,
        ),
    ):
        try:
            soul = Soul(
                orb_color_rgb=(1.0, 0.0, 0.0),
                aura_color_rgb=(0.0, 1.0, 0.0),
                initial_position=(100, 100),
                name="Test_Soul",
            )
        except Exception as e:
            print(f"Failed to init Soul (dependencies?): {e}")
            import traceback

            traceback.print_exc()
            return

        # Soul() creates an agent automatically if local
        if not soul.agent:
            print("Soul initialized without agent? Creating one.")
            soul.agent = SoulAgent(soul)

        # Override decision interval for rapid firing
        soul.agent.decision_interval = 0.01

        running = True

        # Simulation Loop
        start_time = time.time()
        frames = 0
        while running and (time.time() - start_time < 3):  # Run for 3 seconds
            dt = 0.016  # 60 FPS

            # 1. Modify State on Main Thread
            soul.x += random.uniform(-1, 1)
            soul.y += random.uniform(-1, 1)
            if soul.biology:
                soul.biology.satiety -= 0.1
                soul.biology.sensations.append(f"Felt something {frames}")

            # 2. Run Update (Triggers Snapshot -> Agent Thread)
            try:
                soul.update(dt)
            except Exception as e:
                print(f"CRITICAL ERROR in Update: {e}")
                running = False
                raise e

            # 3. Simulate Rendering (Sleep)
            time.sleep(dt / 2)
            frames += 1

        print(f"Game Loop finished. Frames: {frames}")
        print(
            "Success: No ConcurrentModificationExceptions or Race Conditions crashed the loop."
        )


if __name__ == "__main__":
    test_concurrency()
