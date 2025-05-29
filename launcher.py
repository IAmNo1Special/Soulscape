# launcher.py
import pyglet
import random
import ctypes
import sys

from constants import *
from soul import SoulApp
from color_utils import parse_color

active_souls = []
active_windows = []

def create_new_soul_window(window_id, orb_color_input='lightgreen', aura_color_input='orange', name=None):
    new_window = None
    try:
        config = pyglet.gl.Config(
            double_buffer=True,
            depth_size=0,
            alpha_size=8,
            samples=4
        )

        new_window = pyglet.window.Window(
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            caption=f"AudioOrbWindow_{window_id}",
            style=pyglet.window.Window.WINDOW_STYLE_OVERLAY,
            config=config,
            vsync=True,
            resizable=False,
            visible=True,
            fullscreen=False
        )

        screen_width = new_window.screen.width
        screen_height = new_window.screen.height
        pos_x = random.randint(0, max(0, screen_width - WINDOW_WIDTH))
        pos_y = random.randint(0, max(0, screen_height - WINDOW_HEIGHT))
        new_window.set_location(pos_x, pos_y)

        if sys.platform == 'win32':
            try:
                window_title = f"AudioOrbWindow_{window_id}"
                new_window.set_caption(window_title)
                hwnd = ctypes.windll.user32.FindWindowW(None, ctypes.create_unicode_buffer(window_title))
                if not hwnd:
                    print(f"Warning: Could not find window by caption. Falling back to GetForegroundWindow for soul {window_id}.")
                    hwnd = ctypes.windll.user32.GetForegroundWindow()

                GWL_EXSTYLE = -20
                WS_EX_LAYERED = 0x00080000
                WS_EX_TRANSPARENT = 0x00000020
                LWA_ALPHA = 0x2
                LWA_COLORKEY = 0x0001

                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                new_style = (style | WS_EX_LAYERED) & ~WS_EX_TRANSPARENT
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
                ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, LWA_COLORKEY)

                HWND_TOPMOST = -1
                SWP_NOMOVE = 0x0002
                SWP_NOSIZE = 0x0001
                SWP_NOACTIVATE = 0x0010
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 
                    HWND_TOPMOST,
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                )
                print(f"Windows transparency applied (now interactive) for soul {window_id}.")

            except Exception as e:
                print(f"Error applying Windows transparency for soul {window_id}: {e}")
                print("Window might not be fully transparent or interactive.")

    except Exception as e:
        print(f"Error creating Pyglet window for soul {window_id}: {e}")
        if new_window:
            new_window.close()
        return

    try:
        orb_color_rgb = parse_color(orb_color_input)
        aura_color_rgb = parse_color(aura_color_input)
    except Exception as e:
        print(f"Error parsing color for soul {window_id}: {e}. Using default colors.")
        orb_color_rgb = (0.56, 0.93, 0.56)
        aura_color_rgb = (1.0, 0.5, 0.0)

    soul_name = name if name is not None else f"Soul {window_id}"
    soul_app = SoulApp(new_window, orb_color_rgb, aura_color_rgb, soul_name)
    active_souls.append(soul_app)
    active_windows.append(new_window)
    print(f"Spawned new soul in window: {new_window.caption} with orb color {orb_color_input} and aura color {aura_color_input}")

if __name__ == "__main__":
    num_souls_to_spawn = 3
    color = [["darkpurple", "green"], ["red", "black"], ["white", "blue"]]
    soul_names = ["Dekute", "Gorlit", "Zorelle"]

    for i in range(num_souls_to_spawn):
        create_new_soul_window(
            window_id=i + 1, 
            orb_color_input=color[i][0], 
            aura_color_input=color[i][1],
            name=soul_names[i] if i < len(soul_names) else None
        )

    try:
        pyglet.app.run()
    except Exception as e:
        print(f"An error occurred during Pyglet app run: {e}")
    finally:
        for soul_app in active_souls:
            soul_app.cleanup()
        for window_inst in active_windows:
            if not window_inst.has_exit:
                window_inst.close()
        print("All souls and windows cleaned up.")
