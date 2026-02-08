"""GUI Service for handling dialogs and windows in a separate process."""

from __future__ import annotations

import queue
import sys
import traceback
from enum import Enum
from typing import Any

import ttkbootstrap as ttk

from soulscape.ui.gui.message_board_gui import MessageBoardWindow
from soulscape.ui.gui.settings_gui import (
    GlobalSettingsDialog,
    SoulContextMenu,
    SoulSettingsDialog,
)


class GuiCommand(str, Enum):
    """Commands for the GUI service."""

    SHOW_CONTEXT_MENU = "SHOW_CONTEXT_MENU"
    SHOW_SOUL_SETTINGS = "SHOW_SOUL_SETTINGS"
    SHOW_GLOBAL_SETTINGS = "SHOW_GLOBAL_SETTINGS"
    SHOW_ADD_SOUL = "SHOW_ADD_SOUL"
    SHOW_MESSAGE_BOARD = "SHOW_MESSAGE_BOARD"
    CREATE_SOCIAL_POST = "CREATE_SOCIAL_POST"
    CREATE_SOCIAL_REPLY = "CREATE_SOCIAL_REPLY"
    DELETE_SOCIAL_MESSAGE = "DELETE_SOCIAL_MESSAGE"
    EDIT_SOCIAL_MESSAGE = "EDIT_SOCIAL_MESSAGE"
    EXIT = "EXIT"


class GuiService:
    """Service to manage GUI windows and dialogs."""

    def __init__(self, command_queue: Any, result_queue: Any):
        """Initializes the GUI service.

        Args:
            command_queue: Queue for receiving commands.
            result_queue: Queue for sending results back.
        """
        self.command_queue = command_queue
        self.result_queue = result_queue
        self.root: ttk.Window | None = None
        self.active_dialog: Any = None
        self.message_board_window: MessageBoardWindow | None = None
        print("DEBUG: GuiService Initialized")

    def run(self) -> None:
        """Main entry point for the process."""
        # Use ttkbootstrap Window instead of tk.Tk
        self.root = ttk.Window(themename="darkly")
        if self.root:
            self.root.withdraw()  # specific root for the service
            # Poll queue
            self.root.after(10, self._check_queue)
            print("DEBUG: GuiService Loop Starting")
            self.root.mainloop()

    def _check_queue(self) -> None:
        """Checks the command queue for new messages."""
        if not self.root:
            return

        try:
            while True:
                msg = self.command_queue.get_nowait()
                print(f"DEBUG: GuiService received {msg.get('type')}")
                self._handle_command(msg)
        except queue.Empty:
            pass
        except Exception as e:
            print(f"DEBUG: GuiService Error: {e}")
            traceback.print_exc()

        # Schedule next check
        self.root.after(50, self._check_queue)

    def _handle_command(self, msg: dict[str, Any]) -> None:
        """Dispatches commands to handlers.

        Args:
            msg: Command message dictionary.
        """
        cmd_type = msg.get("type")

        if cmd_type == GuiCommand.EXIT and self.root:
            self.root.quit()
            sys.exit(0)

        elif cmd_type == GuiCommand.SHOW_CONTEXT_MENU:
            self._show_context_menu(msg)

        elif cmd_type == GuiCommand.SHOW_SOUL_SETTINGS:
            self._show_soul_settings(msg)

        elif cmd_type == GuiCommand.SHOW_GLOBAL_SETTINGS:
            self._show_global_settings(msg)

        elif cmd_type == GuiCommand.SHOW_ADD_SOUL:
            self._show_add_soul(msg)

        elif cmd_type == GuiCommand.SHOW_MESSAGE_BOARD:
            self._show_message_board(msg)

    def _show_context_menu(self, msg: dict[str, Any]) -> None:
        """Shows the soul context menu.

        Args:
            msg: Message data.
        """
        soul_id = msg.get("soul_id")
        name = msg.get("name")
        x = msg.get("x")
        y = msg.get("y")

        def send_action(action: str) -> None:
            self.result_queue.put(
                {
                    "type": GuiCommand.SHOW_CONTEXT_MENU,
                    "soul_id": soul_id,
                    "action": action,
                }
            )

        menu = SoulContextMenu(
            soul_name=name,  # type: ignore
            on_edit=lambda: send_action("EDIT"),
            on_toggle_aura=lambda: send_action("TOGGLE_AURA"),
            on_dismiss=lambda: send_action("DISMISS"),
            parent=self.root,
        )
        self.active_dialog = menu
        menu.show(x, y)  # type: ignore

        self.result_queue.put(
            {
                "type": GuiCommand.SHOW_CONTEXT_MENU,
                "soul_id": soul_id,
                "action": None,
            }
        )  # Done w/ command processing

    def _show_soul_settings(self, msg: dict[str, Any]) -> None:
        """Shows the soul settings dialog.

        Args:
            msg: Message data.
        """
        soul_id = msg.get("soul_id")

        def on_apply(name: str, orb: str, aura: str) -> None:
            self.result_queue.put(
                {
                    "type": GuiCommand.SHOW_SOUL_SETTINGS,
                    "soul_id": soul_id,
                    "data": {
                        "name": name,
                        "orb_color": orb,
                        "aura_color": aura,
                    },
                }
            )

        d = SoulSettingsDialog(
            parent=self.root,
            name=msg.get("name"),  # type: ignore
            orb_color=msg.get("orb_color"),  # type: ignore
            aura_color=msg.get("aura_color"),  # type: ignore
            stats=msg.get("stats"),  # type: ignore
            on_apply=on_apply,
        )
        d.show()

    def _show_global_settings(self, msg: dict[str, Any]) -> None:
        """Shows the global settings dialog.

        Args:
            msg: Message data.
        """

        def on_apply(opacity: float, startup: bool) -> None:
            self.result_queue.put(
                {
                    "type": GuiCommand.SHOW_GLOBAL_SETTINGS,
                    "data": {"opacity": opacity, "startup": startup},
                }
            )

        d = GlobalSettingsDialog(
            parent=self.root,
            current_opacity=msg.get("current_opacity"),  # type: ignore
            run_on_startup=msg.get("run_on_startup"),  # type: ignore
            on_apply=on_apply,
        )
        d.show()

    def _show_add_soul(self, msg: dict[str, Any]) -> None:
        """Shows the dialog to add a new soul.

        Args:
            msg: Message data.
        """

        def on_apply(name: str, orb: str, aura: str) -> None:
            self.result_queue.put(
                {
                    "type": GuiCommand.SHOW_ADD_SOUL,
                    "data": {
                        "name": name,
                        "orb_color": orb,
                        "aura_color": aura,
                    },
                }
            )

        d = SoulSettingsDialog(parent=self.root, on_apply=on_apply)
        d.show()

    def _show_message_board(self, msg):
        if (
            self.message_board_window is None
            or not self.message_board_window.winfo_exists()
        ):
            # Define callbacks to route requests back to main process
            def on_post(
                author_id: int, author_name: str, title: str, content: str
            ) -> None:
                self.result_queue.put(
                    {
                        "type": GuiCommand.CREATE_SOCIAL_POST,
                        "data": {
                            "author_id": author_id,
                            "author_name": author_name,
                            "title": title,
                            "content": content,
                        },
                    }
                )

            def on_reply(
                author_id: int,
                author_name: str,
                parent_id: str,
                content: str,
            ) -> None:
                self.result_queue.put(
                    {
                        "type": GuiCommand.CREATE_SOCIAL_REPLY,
                        "data": {
                            "author_id": author_id,
                            "author_name": author_name,
                            "parent_id": parent_id,
                            "content": content,
                        },
                    }
                )

            def on_delete(requester_id: int, message_id: str) -> None:
                self.result_queue.put(
                    {
                        "type": GuiCommand.DELETE_SOCIAL_MESSAGE,
                        "data": {
                            "requester_id": requester_id,
                            "message_id": message_id,
                        },
                    }
                )

            def on_edit(
                requester_id: int, message_id: str, content: str
            ) -> None:
                self.result_queue.put(
                    {
                        "type": GuiCommand.EDIT_SOCIAL_MESSAGE,
                        "data": {
                            "requester_id": requester_id,
                            "message_id": message_id,
                            "content": content,
                        },
                    }
                )

            self.message_board_window = MessageBoardWindow(
                self.root,
                on_post=on_post,
                on_reply=on_reply,
                on_delete=on_delete,
                on_edit=on_edit,
            )
            # It's a Toplevel, so we don't need to call show() unless we made it that way
            # MessageBoardWindow.__init__ calls super().__init__ which creates the window.
        else:
            self.message_board_window.lift()


def run_gui_service(command_queue: Any, result_queue: Any) -> None:
    """Process entry point.

    Args:
        command_queue: Queue for commands.
        result_queue: Queue for results.
    """
    service = GuiService(command_queue, result_queue)
    service.run()
