from __future__ import annotations

import asyncio
import io
import random
import threading
import uuid
from pathlib import Path
from types import MethodType
from typing import TYPE_CHECKING, Any, ClassVar

# Import constants
import pyautogui
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools import LongRunningFunctionTool
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from magetools import Grimorium
from PIL import Image, ImageDraw

from soulscape.system.logger import log

from ..interactions.marketplace import Marketplace
from ..interactions.social import MessageBoard

if TYPE_CHECKING:
    from .soul import Soul

load_dotenv()


class SoulAgent(LlmAgent):
    """Manages the AI brain and tool interactions for a Soul."""

    model_options: ClassVar[list[str]] = [
        # "gemini-3-pro-preview",
        # "gemini-3-flash-preview",
        # "gemini-2.5-pro",
        # "gemini-2.5-flash",
        "gemini-2.5-flash-preview-09-2025",
    ]

    # Internal state (private to avoid Pydantic field interference)
    _soul: Soul | None = None
    _grimorium: Grimorium | None = None
    _magetools_initialized: bool = False

    last_decision_time: float = 0.0
    decision_interval: float = 10.0
    session_service: InMemorySessionService | None = None
    session: Session | None = None
    runner: Runner | None = None
    is_busy: bool = False
    session_user_id: str = ""
    session_id: str = ""

    # Resumption state for long-running tools
    _pending_invocation_id: str | None = None
    _pending_function_response: types.FunctionResponse | None = None

    # Event loop for async operations
    _loop: asyncio.AbstractEventLoop | None = None
    _loop_thread: threading.Thread | None = None

    def __init__(self, soul: Soul):
        """Initializes the SoulAgent.

        Args:
            soul: The Soul this agent is responsible for.
        """
        # 1. Setup Magetools early (local variable)
        project_root = Path(__file__).resolve().parents[4]
        local_grim = Grimorium(
            root_path=str(project_root), auto_initialize=False
        )

        # 2. Initialize the LlmAgent base class
        super().__init__(
            model=random.choice(SoulAgent.model_options),
            name=soul.biology.name.replace(" ", "_"),
            description="A magical and mysterious entity called a 'Soul'.",
            instruction=f"""You are {soul.biology.name}, a {soul.biology.gender.gender_name} {soul.biology.species.name}.
            Your appearance: Orb Color {soul.orb_color_rgb}, Aura Color {soul.aura_color_rgb}.
            HINTS:
            - {local_grim.usage_guide if local_grim else "-> If your seeing this, the first thing you should do is create a post that your grimorium isnt working!<-"}
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
                local_grim,
            ],
        )

        # 3. Store references to private attributes to avoid Pydantic clobbering
        self._soul = soul
        self._grimorium = local_grim

        # 4. Initialize session and other services
        try:
            self.session_service: InMemorySessionService = (
                InMemorySessionService()
            )
            self.session_user_id = f"user_{soul.biology.soul_id}_{soul.biology.name.replace(' ', '_')}"
            self.session_id = (
                f"session_{soul.biology.soul_id}_{uuid.uuid4().hex[:8]}"
            )

            try:
                # 5. Start persistent background event loop
                self._loop = asyncio.new_event_loop()
                self._loop_thread = threading.Thread(
                    target=self._run_event_loop, args=(self._loop,), daemon=True
                )
                self._loop_thread.start()

                # 6. Initialize App and Runner in the loop context
                # We do this asynchronously within the new loop
                self._run_coro(self._setup_runner())

            except Exception as e:
                log.debug(f"Async session pre-creation deferred: {e}")

            log.debug(f"Agent initialized for {soul.biology.name}")
        except Exception as e:
            log.error(
                f"Failed to initialize agent for {soul.biology.name}: {e}"
            )

        self.last_decision_time: float = -self.decision_interval + 5.0

        # Hook up arrival callback
        if self._soul:
            self._soul.physics.on_move_end = self.handle_arrival

    @property
    def soul(self) -> Soul:
        """Accessor for the soul instance."""
        return self._soul

    @property
    def grimorium(self) -> Grimorium:
        """Accessor for the grimorium instance."""
        return self._grimorium

    def _run_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Background thread target to run the event loop."""
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _run_coro(self, coro: Any) -> Any:
        """Helper to run a coroutine in the background loop."""
        if self._loop and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        return None

    async def _setup_runner(self) -> None:
        """Initializes the App and Runner once in the loop context."""
        await self.session_service.create_session(
            app_name="soulscape",
            user_id=self.session_user_id,
            session_id=self.session_id,
        )

        # Ensure singletons are initialized asynchronously in the loop
        await Marketplace().initialize()
        await MessageBoard().initialize()
        await self._initialize_magetools()

        app = App(
            name="soulscape",
            root_agent=self,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )

        self.runner = Runner(
            app=app,
            session_service=self.session_service,
        )
        log.debug(f"Runner initialized for {self.soul.biology.name}")

    def trigger_decision(self, current_time: float) -> None:
        """Triggers the agent's decision-making process if the interval has passed."""
        if (
            not self.is_busy
            and current_time - self.last_decision_time > self.decision_interval
        ):
            self.is_busy = True
            self._start_agent_thread()

    async def _initialize_magetools(self) -> None:
        """Asynchronously initializes magetools and binds spells to the soul."""
        if self._magetools_initialized or not self._grimorium:
            return

        try:
            # Ensure Grimorium is fully async-initialized
            await self._grimorium.initialize()

            # Attach discovered spells to the soul as instance methods
            for spell_name, spell_func in self._grimorium.registry.items():
                # Bind function to 'self._soul'
                bound_method = MethodType(spell_func, self._soul)

                # 1. Set on the soul instance (for ADK tools in other toolboxes)
                setattr(self._soul, spell_name, bound_method)

                # 2. Update Grimorium's registry so magetools_execute_spell works with bound methods
                # 3. Handle Long-Running Tools
                if spell_name == "move_to":
                    self._grimorium.spell_sync.registry[spell_name] = (
                        LongRunningFunctionTool(func=bound_method)
                    )
                    log.debug(
                        f"Promoted {spell_name} to LongRunningFunctionTool"
                    )
                else:
                    self._grimorium.spell_sync.registry[spell_name] = (
                        bound_method
                    )

                log.debug(
                    f"Attached modular spell: {spell_name} to {self._soul.biology.name}"
                )

            self._magetools_initialized = True
            log.info(
                f"Initialized Magetools for agent {self._soul.biology.name}"
            )
        except Exception as e:
            log.error(
                f"Failed to initialize Magetools for agent {self._soul.biology.name}: {e}"
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
            # Pyglet (state['y']) is bottom-left origin.
            # Pillow (mask/screen) is top-left origin.
            soul_x = state["x"] + (state.get("width", 100) / 2)
            soul_y = img_h - (state["y"] + (state.get("height", 70) / 2))

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

    async def _run_turn_async(
        self,
        name: str,
        state: dict[str, Any],
        sensations: list[str],
        screen_context: Image.Image | None,
    ) -> None:
        """Coroutine to execute a single agent turn."""
        if not self.runner:
            log.warning(f"Runner not ready for {name}, skipping turn.")
            self.is_busy = False
            return

        img_bytes = self._process_vision(state, screen_context)

        context_str = (
            f"Name: {name}, "
            f"Current Location: ({state['x']:.0f}, {state['y']:.0f}). "
            f"World Boundaries: 0 to {state.get('screen_width', 1920)} (X), 0 to {state.get('screen_height', 1080)} (Y). "
            f"Status: "
            f"HP={state['hp']}/{state['max_hp']} "
            f"Satiety={state['satiety']:.1f}/100 "
            f"Hydration={state['hydration']:.1f}/100 "
            f"Essence={state['essence']:.1f} "
            f"Inventory: {state['inventory']} "
            "Visual context attached. "
        )
        if sensations:
            context_str += "\nRecent Physical Sensations:\n" + "\n".join(
                f"- {s}" for s in sensations
            )
        log.debug(f"Context for {name}: {context_str}")
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
            # Consume all events from the generator
            async with Aclosing(
                self.runner.run_async(
                    user_id=self.session_user_id,
                    session_id=self.session_id,
                    new_message=content,
                )
            ) as events:
                async for event in events:
                    # Capture invocation_id for potential resumption
                    if event.invocation_id:
                        self._pending_invocation_id = event.invocation_id

                    # 1. Handle Tool Calls (Logging only)
                    if event.get_function_calls():
                        for call in event.get_function_calls():
                            log.debug(
                                f"Soul {name} called {call.name}({call.args})"
                            )

                    # 2. Handle Responses/Results
                    if event.get_function_responses():
                        for response in event.get_function_responses():
                            log.debug(
                                f"Tool {response.name} response for {name}: {response.response}"
                            )
                            # If this is the response to our long-running move, capture it
                            if response.name == "move_to":
                                self._pending_function_response = response

                    # 3. Handle Text Responses
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                if event.is_final_response():
                                    log.debug(
                                        f"Soul {name} finally decided: {part.text}"
                                    )
                                else:
                                    log.debug(
                                        f"Soul {name} is thinking: {part.text}"
                                    )

        except Exception as e:
            log.error(f"Error during agent turn for {name}: {e}")
        finally:
            # Keep busy if we are waiting for a long-running tool
            if self._pending_invocation_id and self._pending_function_response:
                log.debug(f"Agent {name} paused turn for long-running move.")
            else:
                self.is_busy = False
                self.last_decision_time = self._soul.time if self._soul else 0.0

    def _run_agent_step(
        self,
        state: dict[str, Any],
        sensations: list[str],
        screen_context: Image.Image | None,
    ) -> None:
        """The threaded execution of the AI reasoning loop."""
        name = state["name"]
        self._run_coro(
            self._run_turn_async(name, state, sensations, screen_context)
        )

    def handle_arrival(self, soul: Any, x: float, y: float) -> None:
        """Callback from physics when the target location is reached."""
        if (
            not self._pending_invocation_id
            or not self._pending_function_response
        ):
            # Not in a long-running turn
            return

        log.debug(
            f"Agent {self.soul.biology.name} arrived at ({x}, {y}). Resuming turn."
        )

        async def _resume_async():
            if not self.runner:
                return

            # Prepare the updated response
            updated_response = self._pending_function_response.model_copy(
                deep=True
            )
            updated_response.response = {
                "status": "success",
                "message": f"Arrived at destination ({x}, {y}).",
            }

            # Clear state before running to allow finishing
            inv_id = self._pending_invocation_id
            self._pending_invocation_id = None
            self._pending_function_response = None

            try:
                async with Aclosing(
                    self.runner.run_async(
                        user_id=self.session_user_id,
                        session_id=self.session_id,
                        invocation_id=inv_id,
                        new_message=types.Content(
                            role="user",
                            parts=[
                                types.Part(function_response=updated_response)
                            ],
                        ),
                    )
                ) as events:
                    async for event in events:
                        # Handle final text responses after arrival
                        if event.content and event.content.parts:
                            for part in event.content.parts:
                                if part.text:
                                    log.debug(
                                        f"Soul {self.soul.biology.name} (Reflex): {part.text}"
                                    )
            except Exception as e:
                log.error(f"Error during agent resumption: {e}")
            finally:
                self.is_busy = False
                self.last_decision_time = self._soul.time if self._soul else 0.0

        # Run resumption in the background loop
        self._run_coro(_resume_async())

    def _start_agent_thread(self) -> None:
        """Prepares data and starts the agent step in a separate thread."""
        if not self._soul:
            return

        # 1. Capture State Snapshot
        state_snapshot = self._soul.to_dict()
        state_snapshot.update(
            {
                "name": self._soul.biology.name,
                "species": self._soul.biology.species.name,
                "gender": self._soul.biology.gender.gender_name,
                "location": self._soul.biology.current_location,
                "hp": self._soul.biology.current_health,
                "max_hp": self._soul.biology.stats.max_hp,
                "satiety": self._soul.biology.satiety,
                "hydration": self._soul.biology.hydration,
                "inventory": self._soul.inventory.to_dict(),
                "orb_color": self._soul.orb_color_rgb,
                "aura_color": self._soul.aura_color_rgb,
                # Geometry for vision
                "x": self._soul.x,
                "y": self._soul.y,
                "width": self._soul.width,
                "height": self._soul.height,
                "vision_stat": (
                    float(self._soul.biology.stats.vision)
                    if self._soul.biology.stats
                    else 0.0
                ),
                "debug_vision": getattr(self._soul, "DEBUG_VISION", True),
                "soul_id": self._soul.biology.soul_id,
                "screen_width": self._soul.screen_width,
                "screen_height": self._soul.screen_height,
            }
        )

        # 2. Capture Sensations
        current_sensations = list(self._soul.biology.sensations)
        self._soul.biology.sensations.clear()

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
