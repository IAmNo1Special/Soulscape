from __future__ import annotations

import asyncio
import io
import random
import threading
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

# Import constants
import pyautogui
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools import LongRunningFunctionTool
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from PIL import Image, ImageDraw

from soulscape.system.logger import log

from ..interactions.marketplace import Marketplace
from ..interactions.social import MessageBoard

if TYPE_CHECKING:
    from .soul import Soul


class SoulAgent(LlmAgent):
    """Manages the AI brain and tool interactions for a Soul."""

    model_options: ClassVar[list[str]] = [
        # "gemini-3-pro-preview",
        # "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-preview-09-2025",
    ]
    soul: Soul | None = None
    last_decision_time: float = 0.0
    decision_interval: float = 60.0
    session_service: InMemorySessionService | None = None
    session: Session | None = None
    runner: Runner | None = None
    is_thinking: bool = False
    session_user_id: str = ""
    session_id: str = ""

    def __init__(self, soul: Soul):
        """Initializes the SoulAgent.

        Args:
            soul: The Soul this agent is responsible for.
        """
        self._init_llm_agent(soul)
        self.soul: Soul = soul

        try:
            self.session_service: InMemorySessionService = (
                InMemorySessionService()
            )
            # Define consistent session identifiers
            self.session_user_id = f"user_{self.soul.biology.soul_id}_{self.soul.biology.name.replace(' ', '_')}"
            self.session_id = (
                f"session_{self.soul.biology.soul_id}_{uuid.uuid4().hex[:8]}"
            )

            try:
                self.session = asyncio.run(
                    self.session_service.create_session(
                        app_name="soulscape",
                        user_id=self.session_user_id,
                        session_id=self.session_id,
                    )
                )

            except RuntimeError:
                # Handle running in existing loop if necessary
                self.session: Session | None = (
                    None  # Requires async handling if in existing loop
                )
            # Initialize ADK Runner
            # Note: In a real refactor, the runner might be shared or managed externally
            # but for compatibility with current Soul implementation:
            self.runner: Runner | None = Runner(
                agent=self,
                app_name="soulscape",
                session_service=self.session_service,
            )
            log.info(f"Agent initialized for {self.soul.biology.name}")
        except Exception as e:
            log.error(
                f"Failed to initialize agent for {self.soul.biology.name}: {e}"
            )

        # Initialize last_decision_time to trigger the first decision immediately
        # But add a small delay (e.g. 5s) to allow the app to fully load/render first
        self.last_decision_time: float = -self.decision_interval + 5.0

    def trigger_decision(self, current_time: float) -> None:
        """Triggers the agent's decision-making process if the interval has passed."""
        if (
            not self.is_thinking
            and current_time - self.last_decision_time > self.decision_interval
        ):
            self.is_thinking = True
            self._start_agent_thread()

    def _init_llm_agent(self, soul: Soul) -> LlmAgent:
        """Initializes the LlmAgent with tools."""

        super().__init__(
            model=random.choice(SoulAgent.model_options),
            name=soul.biology.name.replace(" ", "_"),
            description="A magical and mysterious entity called a 'Soul'.",
            instruction=f"""You are {soul.biology.name}, a {soul.biology.gender.gender_name} {soul.biology.species.name}.
            Your appearance: Orb Color {soul.orb_color_rgb}, Aura Color {soul.aura_color_rgb}.
            HINTS:
            - Satiety/Hydration < 20: You will suffer random health (HP) penalties due to starvation or dehydration.
            - HP <= 0: You will PERISH.
            - Movement: You can travel to any screen coordinates via tools.
            - Inventory: You have a capacity of 10 items.
            - Social: You can communicate with other souls via the Message Board.
            * Posting a new thread costs 5.00 Essence.
            * Replying to a thread costs 2.00 Essence.
            * Reading is free. Check it often (`social_read`) to find friends, trade partners, or share knowledge.
            Make sure you only ever use tools sequentially. NEVER use one or more tools in parallel.

            YOUR GOAL IS WHAT YOU DECIDE IT IS. WELCOME TO THE WORLD! 
            """,
            tools=[
                soul.eat,
                soul.drink,
                soul.find_food,
                soul.find_water,
                LongRunningFunctionTool(func=soul.move_to),
                soul.look_around,
                soul.market_sell,
                soul.market_browse,
                soul.market_buy,
                soul.market_cancel,
                soul.social_post,
                soul.social_read,
                soul.social_reply,
                soul.social_edit,
                soul.social_delete,
                soul.wait_x_secs,
                soul.cancel_action,
            ],
        )

    def _process_vision(
        self, state: dict[str, Any], screen: Image.Image | None
    ) -> bytes | None:
        """Processes the screenshot into a masked vision disk for the agent."""
        if not screen:
            return None

        try:
            vision_radius = max(100, int(state["vision_stat"] * 1.5))
            img_w, img_h = screen.size

            # center
            soul_x = state["x"] + 50
            soul_y = state["y"] + 35  # Approximation

            mask = Image.new("L", (img_w, img_h), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse(
                (
                    soul_x - vision_radius,
                    soul_y - vision_radius,
                    soul_x + vision_radius,
                    soul_y + vision_radius,
                ),
                fill=255,
            )

            black_bg = Image.new("RGB", (img_w, img_h), (0, 0, 0))
            masked_img = Image.composite(screen, black_bg, mask)

            # thumbnail for efficiency
            masked_img.thumbnail((800, 600))
            img_byte_arr = io.BytesIO()
            masked_img.save(img_byte_arr, format="PNG")
            return img_byte_arr.getvalue()
        except Exception as e:
            log.warning(f"Vision processing failed: {e}")
            return None

    def _run_agent_step(
        self,
        state: dict[str, Any],
        sensations: list[str],
        screen_context: Image.Image | None,
    ) -> None:
        """The threaded execution of the AI reasoning loop."""
        name = state["name"]

        async def _run_async_internal():
            # Ensure singletons are initialized asynchronously
            await Marketplace().initialize()
            await MessageBoard().initialize()

            img_bytes = self._process_vision(state, screen_context)

            context_str = (
                f"Name: {name}, "
                f"Current Location: ({state['x']:.0f}, {state['y']:.0f}). "
                f"Status: HP={state['hp']}/{state['max_hp']}, "
                f"Satiety={state['satiety']:.1f}/100, Hydration={state['hydration']:.1f}/100. "
                f"Essence={state['essence']:.1f}. "
                f"Inventory: {state['inventory']}."
                "Visual context attached."
            )
            if sensations:
                context_str += "\nRecent Physical Sensations:\n" + "\n".join(
                    f"- {s}" for s in sensations
                )

            parts = [types.Part(text=context_str)]
            if img_bytes:
                parts.append(
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png", data=img_bytes
                        )
                    )
                )

            content = types.Content(role="user", parts=parts)

            try:
                async with Aclosing(
                    self.runner.run_async(
                        user_id=self.session_user_id,
                        session_id=self.session_id,
                        new_message=content,
                    )
                ) as events:
                    async for event in events:
                        if event.long_running_tool_ids:
                            # Handle long-running tools (like move_to)
                            for call_id in event.long_running_tool_ids:
                                # Poll for completion. For soulscape, move_to finishes when physics target is None.
                                log.info(
                                    f"Agent {name} waiting for long-running tool {call_id}..."
                                )
                                while (
                                    self.soul
                                    and self.soul.physics.roaming_target
                                    is not None
                                ):
                                    await asyncio.sleep(
                                        1.0
                                    )  # Poll every second

                                # Send final response once arrived
                                log.info(
                                    f"Agent {name} arrived at destination. Resuming..."
                                )
                                final_res = types.FunctionResponse(
                                    id=call_id,
                                    name="move_to",
                                    response={
                                        "status": "success",
                                        "message": "Arrived at destination.",
                                    },
                                )
                                # We continue the run with the response
                                async with Aclosing(
                                    self.runner.run_async(
                                        user_id=self.session_user_id,
                                        session_id=self.session_id,
                                        new_message=types.Content(
                                            role="user",
                                            parts=[
                                                types.Part(
                                                    function_response=final_res
                                                )
                                            ],
                                        ),
                                    )
                                ) as sub_events:
                                    async for sub_event in sub_events:
                                        if (
                                            sub_event.is_final_response()
                                            and sub_event.content.parts
                                        ):
                                            for part in sub_event.content.parts:
                                                if part.text:
                                                    log.info(
                                                        f"Soul {name} decided after move: {part.text}"
                                                    )

                        if event.is_final_response() and event.content.parts:
                            for part in event.content.parts:
                                if part.text:
                                    log.info(
                                        f"Soul {name} decided: {part.text}"
                                    )
            except Exception as e:
                log.error(f"Error during agent turn for {name}: {e}")
            finally:
                self.is_thinking = False
                self.last_decision_time = self.soul.time if self.soul else 0.0

        asyncio.run(_run_async_internal())

    def _start_agent_thread(self) -> None:
        """Prepares data and starts the agent step in a separate thread."""
        # 1. Capture State Snapshot
        state_snapshot = self.soul.to_dict()
        state_snapshot.update(
            {
                "name": self.soul.biology.name,
                "species": self.soul.biology.species.name,
                "gender": self.soul.biology.gender.gender_name,
                "location": self.soul.biology.current_location,
                "hp": self.soul.biology.current_health,
                "max_hp": self.soul.biology.stats.max_hp,
                "satiety": self.soul.biology.satiety,
                "hydration": self.soul.biology.hydration,
                "inventory": self.soul.inventory.to_dict(),
                "orb_color": self.soul.orb_color_rgb,
                "aura_color": self.soul.aura_color_rgb,
                # Geometry for vision
                "x": self.soul.x,
                "y": self.soul.y,
                "width": self.soul.width,
                "height": self.soul.height,
                "vision_stat": (
                    float(self.soul.biology.stats.vision)
                    if self.soul.biology.stats
                    else 0.0
                ),
                "debug_vision": getattr(self.soul, "DEBUG_VISION", True),
                "soul_id": self.soul.biology.soul_id,
            }
        )

        # 2. Capture Sensations
        current_sensations = list(self.soul.biology.sensations)
        self.soul.biology.sensations.clear()

        # 3. Capture Screen Context
        try:
            screen_context = pyautogui.screenshot()
        except Exception as e:
            log.error(f"Screenshot failed: {e}")
            screen_context = None

        # 4. Spawn Thread
        threading.Thread(
            target=self._run_agent_step,
            args=(state_snapshot, current_sensations, screen_context),
            daemon=True,
        ).start()
