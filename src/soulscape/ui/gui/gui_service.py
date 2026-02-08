import queue
import sys
import traceback
from enum import Enum

import ttkbootstrap as ttk

from soulscape.ui.gui.message_board_gui import MessageBoardWindow

# Import dialogs
# Note: We must ensure these imports don't trigger Pyglet init
from soulscape.ui.gui.settings_gui import (
    GlobalSettingsDialog,
    SoulContextMenu,
    SoulSettingsDialog,
)


class GuiCommand(str, Enum):
    SHOW_CONTEXT_MENU = "SHOW_CONTEXT_MENU"
    SHOW_SOUL_SETTINGS = "SHOW_SOUL_SETTINGS"
    SHOW_GLOBAL_SETTINGS = "SHOW_GLOBAL_SETTINGS"
    SHOW_ADD_SOUL = "SHOW_ADD_SOUL"
    SHOW_MESSAGE_BOARD = "SHOW_MESSAGE_BOARD"
    EXIT = "EXIT"


class GuiService:
    def __init__(self, command_queue, result_queue):
        self.command_queue = command_queue
        self.result_queue = result_queue
        self.root = None
        self.active_dialog = None
        self.message_board_window = None
        print("DEBUG: GuiService Initialized")

    def run(self):
        """Main entry point for the process."""
        # Use ttkbootstrap Window instead of tk.Tk
        self.root = ttk.Window(themename="darkly")
        self.root.withdraw()  # specific root for the service

        # Poll queue
        self.root.after(10, self._check_queue)

        print("DEBUG: GuiService Loop Starting")
        self.root.mainloop()

    def _check_queue(self):
        try:
            # consuming all available commands? Or just one at a time?
            # Better one at a time if they are blocking dialogs?
            # But the queue check shouldn't block.
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

    def _handle_command(self, msg):
        cmd_type = msg.get("type")

        if cmd_type == GuiCommand.EXIT:
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

    def _show_context_menu(self, msg):
        # We need a temporary handler to capture the result
        soul_id = msg.get("soul_id")
        name = msg.get("name")
        x = msg.get("x")
        y = msg.get("y")

        def send_action(action):
            self.result_queue.put(
                {
                    "type": GuiCommand.SHOW_CONTEXT_MENU,
                    "soul_id": soul_id,
                    "action": action,
                }
            )

        menu = SoulContextMenu(
            soul_name=name,
            on_edit=lambda: send_action("EDIT"),
            on_toggle_aura=lambda: send_action("TOGGLE_AURA"),
            on_dismiss=lambda: send_action("DISMISS"),
            parent=self.root,
        )
        # Note: menu.show blocks in the original implementation?
        # SoulContextMenu.show creates a NEW root and mainloop.
        # We need to adapt logic to use OUR root if possible, or tolerate the nested loop.
        # The current implementation of SoulContextMenu DOES create a new Tk() and mainloop().
        # This is bad for a persistent service. We should refactor SoulContextMenu to use Toplevel if root exists.

        # For now, let's try running it. If it creates a new root, it might conflict or just work as a modal.
        # Ideally, we refactor SoulContextMenu to NOT create a root if one exists.
        # But given we want fast response, let's assume valid Toplevel usage.

        # WAIT: SoulContextMenu currently does `self.root = tk.Tk()`. We must change that.
        # But I can't easily change `settings_gui.py` without risking breaking `launcher.py` if it relies on it?
        # `launcher.py` spawned a thread, so it was fine making a new root.

        # Actually, if I run `menu.show(x, y)`, it blocks until closed.
        # Since this is a service process, blocking the service loop is actually FINE for a modal menu!
        # It just means we won't process other messages until the menu closes. That's expected for a modal.
        # Store to prevent GC
        self.active_dialog = menu

        menu.show(x, y)
        self.result_queue.put(
            {
                "type": GuiCommand.SHOW_CONTEXT_MENU,
                "soul_id": soul_id,
                "action": None,
            }
        )  # Done w/ command processing

    def _show_soul_settings(self, msg):
        soul_id = msg.get("soul_id")

        def on_apply(name, orb, aura):
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
            name=msg.get("name"),
            orb_color=msg.get("orb_color"),
            aura_color=msg.get("aura_color"),
            stats=msg.get("stats"),
            on_apply=on_apply,
        )
        d.show()

    def _show_global_settings(self, msg):
        def on_apply(opacity, startup):
            self.result_queue.put(
                {
                    "type": GuiCommand.SHOW_GLOBAL_SETTINGS,
                    "data": {"opacity": opacity, "startup": startup},
                }
            )

        d = GlobalSettingsDialog(
            parent=self.root,
            current_opacity=msg.get("current_opacity"),
            run_on_startup=msg.get("run_on_startup"),
            on_apply=on_apply,
        )
        d.show()

    def _show_add_soul(self, msg):
        def on_apply(name, orb, aura):
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

        # show_add_soul_dialog just instantiates SoulSettingsDialog with on_apply
        # We can do that manually
        d = SoulSettingsDialog(parent=self.root, on_apply=on_apply)
        d.show()

    def _show_message_board(self, msg):
        if (
            self.message_board_window is None
            or not self.message_board_window.winfo_exists()
        ):
            self.message_board_window = MessageBoardWindow(self.root)
            # It's a Toplevel, so we don't need to call show() unless we made it that way
            # MessageBoardWindow.__init__ calls super().__init__ which creates the window.
        else:
            self.message_board_window.lift()


def run_gui_service(command_queue, result_queue):
    """Process entry point."""
    service = GuiService(command_queue, result_queue)
    service.run()
