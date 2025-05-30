# color_utils.py
import re

def hex_to_rgb(hex_color):
    """Converts a hex color string (e.g., '#RRGGBB' or '#RGB') to an RGB tuple (0.0-1.0)."""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join([c*2 for c in hex_color])

    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return (r, g, b)
    except ValueError:
        raise ValueError(f"Invalid hex color format: {hex_color}")

def parse_color(color_input):
    """
    Parses various color inputs (name, hex, RGB tuple) into an RGB tuple (0.0-1.0).
    
    Args:
        color_input (str or tuple): Color name (e.g., 'red'), hex code (e.g., '#FF0000'),
                                     or an RGB tuple (e.g., (1.0, 0.0, 0.0)).
                                     RGB tuples can be 0-255 or 0.0-1.0.
    
    Returns:
        tuple: An RGB tuple with float values between 0.0 and 1.0.
        
    Raises:
        ValueError: If the color input format is not recognized or invalid.
    """
    if isinstance(color_input, tuple):
        if len(color_input) != 3:
            raise ValueError(f"RGB tuple must have 3 values (R, G, B): {color_input}")
        if any(c > 1.0 for c in color_input):
            return (color_input[0] / 255.0, color_input[1] / 255.0, color_input[2] / 255.0)
        else:
            return color_input

    elif isinstance(color_input, str):
        color_input = color_input.strip().lower()
        named_colors = {
            'red': (1.0, 0.0, 0.0),
            'green': (0.0, 1.0, 0.0),
            'blue': (0.0, 0.0, 1.0),
            'white': (1.0, 1.0, 1.0),
            'black': (0.0, 0.0, 0.0),
            'yellow': (1.0, 1.0, 0.0),
            'cyan': (0.0, 1.0, 1.0),
            'magenta': (1.0, 0.0, 1.0),
            'orange': (1.0, 0.5, 0.0),
            'purple': (0.5, 0.0, 0.5),
            'pink': (1.0, 0.75, 0.8),
            'lightgreen': (0.56, 0.93, 0.56),
            'darkgreen': (0.0, 0.5, 0.0),
            'lightblue': (0.68, 0.85, 0.9),
            'darkblue': (0.0, 0.0, 0.5),
            'darkorange': (0.8, 0.3, 0.0),
            'darkpurple': (0.3, 0.0, 0.3),
            'crimson': (0.86, 0.08, 0.23),
            'indigo': (0.29, 0.0, 0.51),
            'navy': (0.0, 0.0, 0.5),
            'chocolate': (0.82, 0.41, 0.12),
            'sienna': (0.63, 0.32, 0.18),
            'peru': (0.8, 0.52, 0.25),
            'firebrick': (0.7, 0.13, 0.13),
            'forestgreen': (0.13, 0.55, 0.13),
            'skyblue': (0.53, 0.81, 0.92),
            'gold': (1.0, 0.84, 0.0),
            'gray': (0.5, 0.5, 0.5)
        }
        if color_input in named_colors:
            return named_colors[color_input]
        if re.match(r'^#([0-9a-fA-F]{3}){1,2}$', color_input):
            return hex_to_rgb(color_input)

    raise ValueError(f"Unrecognized color format: {color_input}. Expected name, hex, or RGB tuple.")
