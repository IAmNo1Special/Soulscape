import asyncio
import os
import sys
import traceback

# Fix path
HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HUB_ROOT not in sys.path:
    sys.path.insert(0, HUB_ROOT)

from .. import main


async def main_debug():
    try:
        # Initialize DB
        main.init_db()

        payload = {
            "owner_id": "user1",
            "souls": [
                {
                    "soul_id": "3",
                    "name": "C",
                    "position": [10, 20],
                    "hometown": {"x": 1, "y": 1},
                    "orb_color": [255, 0, 0],
                    "aura_color": [0, 255, 0],
                    "inventory": {"items": [{"name": "Coal", "quantity": 10}]},
                    "essence": 500.0,
                    "hp": 200,
                    "max_hp": 200,
                    "satiety": 80,
                    "hydration": 90,
                    "level": 10,
                    "xp": 500,
                }
            ],
        }

        # Call the endpoint function directly
        result = await main.update_souls(payload)
        print("Update result:", result)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main_debug())
    asyncio.run(main_debug())
