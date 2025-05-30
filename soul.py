# soul.py
import pyglet
from pyglet.gl import *
from pyglet.graphics import Batch, Group
from pyglet.text import Label as PygletLabel
from pyglet.window import key, mouse
from pyglet import math as pmath
import numpy as np
import pyautogui
import random
import math
import ctypes # For Windows-specific window behavior
import time  # For double click timing

# Import constants and shader utilities
from constants import *
from utils.shader_utils import create_shader_program
    

# --- Geometry Generation ---
def create_sphere(radius, lat_segments, long_segments):
    """
    Generates vertex, normal, and index data for a sphere.

    Args:
        radius (float): The radius of the sphere.
        lat_segments (int): Number of segments along the latitude.
        long_segments (int): Number of segments along the longitude.

    Returns:
        dict: A dictionary containing 'vertices', 'normals', and 'indices' arrays.
    """
    vertices = []
    normals = []
    indices = []
    
    # Generate vertices and normals
    for lat in range(lat_segments + 1):
        theta = lat * math.pi / lat_segments
        sin_theta = math.sin(theta)
        cos_theta = math.cos(theta)
        
        for long in range(long_segments + 1):
            phi = long * 2 * math.pi / long_segments
            sin_phi = math.sin(phi)
            cos_phi = math.cos(phi)
            
            x = cos_phi * sin_theta
            y = cos_theta
            z = sin_phi * sin_theta
            
            vertices.extend([x * radius, y * radius, z * radius])
            normals.extend([x, y, z])
    
    # Generate indices
    for lat in range(lat_segments):
        for long in range(long_segments):
            first = (lat * (long_segments + 1)) + long
            second = first + long_segments + 1
            
            indices.extend([first, second, first + 1])
            indices.extend([second, second + 1, first + 1])
    
    return {
        'vertices': (GLfloat * len(vertices))(*vertices),
        'normals': (GLfloat * len(normals))(*normals),
        'indices': (GLuint * len(indices))(*indices)
    }

# --- Renderer Classes ---
class OrbRenderer:
    """
    Handles the rendering of the main orb.
    """
    def __init__(self, base_color_rgb):
        self.program = create_shader_program(ORB_VERTEX_SHADER_PATH, ORB_FRAGMENT_SHADER_PATH)
        sphere_data = create_sphere(ORB_RADIUS, ORB_LAT_SEGMENTS, ORB_LONG_SEGMENTS)
        self.vertex_list = self.program.vertex_list_indexed(
            len(sphere_data['vertices']) // 3, GL_TRIANGLES, sphere_data['indices'],
            position=('f', sphere_data['vertices']), normal=('f', sphere_data['normals'])
        )
        self.base_color_rgb = base_color_rgb # Store the base color

    def draw(self, model, view, projection, time, bulge_position):
        """
        Draws the orb with given matrices and uniform values.
        """
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA) 

        with self.program:
            scaled_orb_model = model.scale((ORB_SCALE, ORB_SCALE, ORB_SCALE))
            positioned_orb_model = scaled_orb_model.translate(pmath.Vec3(0, ORB_Y_OFFSET, 0))
            
            self.program['model'] = positioned_orb_model
            self.program['view'] = view
            self.program['projection'] = projection
            self.program['time'] = time
            self.program['bulge_position'] = bulge_position
            self.program['bulge_strength'] = ORB_BULGE_STRENGTH
            self.program['base_color_uniform'] = self.base_color_rgb
            
            self.vertex_list.draw(GL_TRIANGLES)

class AuraRenderer:
    """
    Handles the rendering of the aura surrounding the orb.
    """
    def __init__(self, base_color_rgb):
        self.program = create_shader_program(AURA_VERTEX_SHADER_PATH, AURA_FRAGMENT_SHADER_PATH)
        aura_data = create_sphere(AURA_RADIUS, AURA_LAT_SEGMENTS, AURA_LONG_SEGMENTS)
        self.vertex_list = self.program.vertex_list_indexed(
            len(aura_data['vertices']) // 3, GL_TRIANGLES, aura_data['indices'],
            position=('f', aura_data['vertices']), normal=('f', aura_data['normals'])
        )
        self.base_color_rgb = base_color_rgb

    def draw(self, model, view, projection, time, bulge_position):
        """
        Draws the aura with given matrices and uniform values.
        """
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE) 
        glDepthMask(False) 
        glDisable(GL_CULL_FACE)

        with self.program:
            aura_model = model.scale((AURA_SCALE_X, AURA_SCALE_Y, AURA_SCALE_Z))
            
            self.program['model'] = aura_model
            self.program['view'] = view
            self.program['projection'] = projection
            self.program['time'] = time
            self.program['bulge_position'] = bulge_position
            self.program['bulge_strength'] = AURA_BULGE_STRENGTH
            self.program['base_brightness'] = AURA_BASE_BRIGHTNESS
            self.program['base_color_uniform'] = self.base_color_rgb
            
            self.vertex_list.draw(GL_TRIANGLES)

# --- Window Physics and Interaction ---
class WindowPhysics:
    """
    Manages the movement and interaction of a specific Pyglet window.
    """
    def __init__(self, window_ref, name="Soul"):
        self.window = window_ref
        initial_x, initial_y = self.window.get_location()
        self.x = float(initial_x) # Window's top-left X on screen
        self.y = float(initial_y) # Window's top-left Y on screen
        self.vx = 0.0
        self.vy = 0.0
        self.is_dragging = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.roaming_target = None
        self.roaming_pause = 0.0
        self.follow_mouse = False
        self.last_click_time = 0
        self.mouse_x = 0
        self.mouse_y = 0
        self.hover_phase = 0.0  # For hover animation
        self.follow_speed = 0.15  # Speed of following (lower is slower)
        self.max_speed = 0.5  # Maximum speed when following
        self.follow_delay = 0.0  # Timer for follow delay
        self.follow_delay_duration = 0.5  # 0.5 second delay before following starts
        self.name = name
        self.is_hovered = False
        
        # Track previous position for smooth movement
        self.last_x = int(self.x)
        self.last_y = int(self.y)
        
        # Create a label for the name
        self.name_label = pyglet.text.Label(
            name,
            font_name='Arial',
            font_size=12,
            x=WINDOW_WIDTH // 2,  # Center of the window
            y=10,  # Position below the orb
            anchor_x='center',
            anchor_y='center',
            color=(255, 255, 255, 0),  # Start with 0 alpha (invisible)
            batch=window_ref.batch if hasattr(window_ref, 'batch') else None
        )
        
    def _bounce_on_edges(self):
        """Handles bouncing off screen edges with energy loss."""
        screen_width = self.window.screen.width
        screen_height = self.window.screen.height

        if self.x < 0:
            self.x = 0
            self.vx = -self.vx * BOUNCE_COEFFICIENT
        elif self.x + self.window.width > screen_width:
            self.x = screen_width - self.window.width
            self.vx = -self.vx * BOUNCE_COEFFICIENT

        if self.y < 0:
            self.y = 0
            self.vy = -self.vy * BOUNCE_COEFFICIENT
        elif self.y + self.window.height > screen_height:
            self.y = screen_height - self.window.height
            self.vy = -self.vy * BOUNCE_COEFFICIENT
    
    def on_mouse_press(self, x_mouse_relative, y_mouse_relative, button, modifiers):
        """Handles mouse press events to initiate dragging."""
        if button == mouse.LEFT:
            # x_mouse_relative, y_mouse_relative are from window's bottom-left
            if 0 <= x_mouse_relative <= self.window.width and \
               0 <= y_mouse_relative <= self.window.height:
                
                # Get current window screen position (top-left)
                win_screen_x, win_screen_y = self.window.get_location()

                # Check for double click (within 300ms)
                current_time = time.time()
                if current_time - self.last_click_time < 0.3:  # Double click detected
                    self.follow_mouse = not self.follow_mouse
                    print(f"Follow mode: {'ENABLED' if self.follow_mouse else 'DISABLED'}")
                    
                    if self.follow_mouse:
                        # Start follow delay timer
                        self.follow_delay = self.follow_delay_duration
                        # Stop any current movement
                        self.vx = 0.0
                        self.vy = 0.0
                        self.roaming_target = None
                        self.roaming_pause = 0.0
                    return  # Skip dragging on double click
                
                self.last_click_time = current_time
                
                # Start dragging
                self.is_dragging = True
                self.drag_offset_x = x_mouse_relative 
                self.drag_offset_y = self.window.height - y_mouse_relative
                self.vx = 0.0 # Stop independent movement
                self.vy = 0.0
                
                # print(f"Drag Start. Win ({self.x},{self.y}). MouseRel ({x_mouse_relative},{y_mouse_relative}). Offset ({self.drag_offset_x},{self.drag_offset_y})")


    def on_mouse_release(self, x_mouse, y_mouse, button, modifiers):
        """Handles mouse release events to stop dragging."""
        if button == mouse.LEFT:
            self.is_dragging = False
            # print("Drag End")
            
    def on_mouse_enter(self, x, y):
        """Handles mouse enter event to show the name label."""
        self.is_hovered = True
        if hasattr(self, 'name_label'):
            pyglet.clock.unschedule(self.fade_label_out)
            pyglet.clock.schedule_interval(self.fade_label_in, 1/60.0)
    
    def on_mouse_leave(self, x, y):
        """Handles mouse leave event to hide the name label."""
        self.is_hovered = False
        if hasattr(self, 'name_label'):
            pyglet.clock.unschedule(self.fade_label_in)
            pyglet.clock.schedule_interval(self.fade_label_out, 1/60.0)
    
    def fade_label_in(self, dt):
        """Gradually fades in the name label."""
        if hasattr(self, 'name_label'):
            current_alpha = self.name_label.color[3]
            if current_alpha < 255:
                new_alpha = min(255, current_alpha + 15)
                self.name_label.color = (255, 255, 255, new_alpha)
            else:
                pyglet.clock.unschedule(self.fade_label_in)
    
    def fade_label_out(self, dt):
        """Gradually fades out the name label."""
        if hasattr(self, 'name_label'):
            current_alpha = self.name_label.color[3]
            if current_alpha > 0:
                new_alpha = max(0, current_alpha - 15)
                self.name_label.color = (255, 255, 255, new_alpha)
            else:
                pyglet.clock.unschedule(self.fade_label_out)
    
    def on_mouse_motion(self, x, y, dx, dy):
        """Tracks mouse motion to update follow target."""
        # Update mouse position for following
        self.mouse_x = x
        self.mouse_y = y
        
        # Update name label position to stay centered under the orb
        if hasattr(self, 'name_label'):
            self.name_label.x = WINDOW_WIDTH // 2
            self.name_label.y = 10

    def on_mouse_drag(self, x_mouse_relative, y_mouse_relative, dx, dy, buttons, modifiers):
        """
        Handles mouse drag events. Directly updates window position.
        x_mouse_relative, y_mouse_relative are current mouse coords relative to window's bottom-left.
        dx, dy are the change in these window-relative mouse positions.
        """
        if self.is_dragging and (buttons & mouse.LEFT):
            # The new window top-left (self.x, self.y) should be:
            # (current_mouse_absolute_screen_x - self.drag_offset_x, 
            #  current_mouse_absolute_screen_y - self.drag_offset_y)

            # To get current_mouse_absolute_screen_x:
            # We know the window's current top-left is (self.x, self.y).
            # The mouse is at x_mouse_relative from the window's left edge.
            # So, current_mouse_absolute_screen_x = self.x (current window left) + x_mouse_relative.
            current_mouse_abs_x = self.x + x_mouse_relative
            
            # To get current_mouse_absolute_screen_y:
            # Window's top is self.y. Mouse y_mouse_relative is from window bottom.
            # Offset from window top is (self.window.height - y_mouse_relative).
            # So, current_mouse_absolute_screen_y = self.y (current window top) + (self.window.height - y_mouse_relative).
            current_mouse_abs_y = self.y + (self.window.height - y_mouse_relative)
            
            # Update mouse position for following if enabled
            self.mouse_x = current_mouse_abs_x
            self.mouse_y = current_mouse_abs_y

            # Now calculate the new window top-left position
            new_window_x = current_mouse_abs_x - self.drag_offset_x
            new_window_y = current_mouse_abs_y - self.drag_offset_y
            
            self.x = new_window_x
            self.y = new_window_y
            
            # Clamp to screen edges immediately during drag
            screen_width = self.window.screen.width
            screen_height = self.window.screen.height
            self.x = max(0, min(self.x, screen_width - self.window.width))
            self.y = max(0, min(self.y, screen_height - self.window.height))

            self.window.set_location(int(self.x), int(self.y))
            # print(f"Dragging. New Win Pos: ({self.x},{self.y})")


    def update(self, dt):
        """Updates the window's position and physics."""
        
        if self.is_dragging:
            # Position is solely controlled by on_mouse_drag.
            # Ensure velocities remain zeroed if any were accumulated.
            self.vx = 0.0
            self.vy = 0.0
            return  # Skip other physics while dragging
            
        # Handle following the mouse
        if self.follow_mouse and self.follow_delay <= 0.0:
            # Following is active after delay has elapsed
            self._update_follow_mouse(dt)
        elif self.follow_mouse and self.follow_delay > 0.0:
            # Count down the follow delay
            self.follow_delay -= dt
    def _update_follow_mouse(self, dt):
        """Update the window position to follow the mouse."""
        mouse_x, mouse_y = pyautogui.position()
        
        # Calculate target position (center of window should be at mouse position with offset)
        target_x = mouse_x - (self.window.width // 2) - 20  # 20px offset to the left
        target_y = mouse_y - (self.window.height // 2) - 20  # 20px offset up
        
        # Calculate direction vector and distance
        dx = target_x - self.x
        dy = target_y - self.y
        distance_sq = dx*dx + dy*dy
        
        # Define a dead zone where we stop moving (squared for efficiency)
        dead_zone_sq = 4.0  # 2.0 pixels squared
        
        if distance_sq > dead_zone_sq:
            # Calculate actual distance only when needed
            distance = math.sqrt(distance_sq)
            
            # Normalize direction
            inv_distance = 1.0 / distance
            dx *= inv_distance
            dy *= inv_distance
            
            # Calculate speed with smooth deceleration using an ease-out curve
            # This creates a more natural slowdown as we approach the target
            deceleration_distance = 50.0  # Distance over which to decelerate (pixels)
            speed_factor = min(1.0, distance / deceleration_distance)
            
            # Apply a smoother curve (ease-out cubic)
            speed_factor = 1.0 - (1.0 - speed_factor) ** 3
            
            # Calculate speed with frame-rate independence
            speed = self.max_speed * speed_factor
            
            # Calculate movement amount with sub-pixel precision
            move_amount = speed * 60.0 * dt  # 60.0 is our target FPS
            
            # Ensure we don't overshoot the target
            move_amount = min(move_amount, distance - math.sqrt(dead_zone_sq))
            
            # Update position with sub-pixel precision
            self.x += dx * move_amount
            self.y += dy * move_amount
            
            # Get screen bounds once
            screen_width = self.window.screen.width
            screen_height = self.window.screen.height
            window_width = self.window.width
            window_height = self.window.height
            
            # Clamp to screen edges with sub-pixel precision
            self.x = max(0.0, min(self.x, screen_width - window_width))
            self.y = max(0.0, min(self.y, screen_height - window_height))
            
            # Only update window position if it changed by at least 1 pixel
            new_x, new_y = int(round(self.x)), int(round(self.y))
            if (new_x, new_y) != (self.last_x, self.last_y):
                self.window.set_location(new_x, new_y)
                self.last_x, self.last_y = new_x, new_y
            return  # Skip the rest of the update if we're following the mouse

        # --- Roaming behavior when not dragging ---
        if self.roaming_pause > 0:
            self.roaming_pause -= dt
            if self.roaming_pause < 0: self.roaming_pause = 0 # Ensure it doesn't go negative
        else:
            if self.roaming_target is None or random.random() < 0.01: # 1% chance to pick new target
                screen_width = self.window.screen.width
                screen_height = self.window.screen.height
                padding = min(screen_width, screen_height) * 0.1
                
                target_x = random.uniform(padding, screen_width - self.window.width - padding)
                target_y = random.uniform(padding, screen_height - self.window.height - padding)
                self.roaming_target = (target_x, target_y)
            
            if self.roaming_target:
                dx_target = self.roaming_target[0] - self.x
                dy_target = self.roaming_target[1] - self.y
                dist = math.hypot(dx_target, dy_target)
                
                if dist < 25.0: # Reached target
                    self.roaming_target = None
                    self.roaming_pause = random.uniform(ROAM_PAUSE_MIN, ROAM_PAUSE_MAX)
                    self.vx *= 0.5 # Dampen velocity
                    self.vy *= 0.5
                else:
                    if dist > 0: # Avoid division by zero
                        ax = (dx_target / dist) * ROAM_ACCELERATION
                        ay = (dy_target / dist) * ROAM_ACCELERATION
                        self.vx += ax
                        self.vy += ay
        
        # Apply friction
        self.vx *= FRICTION 
        self.vy *= FRICTION
        
        # Limit velocity
        max_v = 5.0 # Reduced max roaming speed for gentler movement
        self.vx = max(-max_v, min(max_v, self.vx))
        self.vy = max(-max_v, min(max_v, self.vy))

        # Update position based on velocity
        self.x += self.vx 
        self.y += self.vy
        
        # Gentle hovering effect
        self.hover_phase += dt * HOVER_FREQUENCY * 2 * math.pi 
        hover_y_offset = math.sin(self.hover_phase) * HOVER_AMPLITUDE
        
        # Handle screen edges with bounce (updates self.x, self.y, self.vx, self.vy)
        self._bounce_on_edges()
        
        # Final position for set_location includes hover, after bounce correction on base self.y
        final_y_pos = self.y + hover_y_offset 
        
        # Clamp final Y position again after hover, just in case hover pushes it out of bounds
        # (though _bounce_on_edges should ideally handle this if self.y was adjusted correctly)
        screen_height = self.window.screen.height 
        final_y_pos = max(0, min(final_y_pos, screen_height - self.window.height))
        final_x_pos = self.x # self.x already corrected by _bounce_on_edges

        self.window.set_location(int(final_x_pos), int(final_y_pos))


# --- Main Application Class ---
class SoulApp:
    """
    Main application class that orchestrates the orb, aura, and window physics
    for a single Pyglet window.
    """
    def __init__(self, window_instance, orb_color_rgb, aura_color_rgb, name=None):
        self.window = window_instance 
        self.orb_renderer = OrbRenderer(orb_color_rgb)
        self.aura_renderer = AuraRenderer(aura_color_rgb)
        self.time = 0.0
        self.bulge_position = [0.0, 0.0, 0.0]
        self.aura_visible = True  # Add flag to track aura visibility

        self.base_camera_distance = 2.0 
        self.base_window_height = float(WINDOW_HEIGHT)

        # Create a batch for efficient drawing
        self.batch = pyglet.graphics.Batch()
        
        # Create window physics with the provided name or default
        soul_name = name if name is not None else f"Soul {len(pyglet.app.windows)}"
        self.window_physics = WindowPhysics(self.window, soul_name)
        
        # Update the name label with the batch
        if hasattr(self.window_physics, 'name_label'):
            self.window_physics.name_label.batch = self.batch
        
        self.window.push_handlers(
            self.on_draw,
            self.on_resize,
            self.window_physics.on_mouse_press,
            self.window_physics.on_mouse_release,
            self.window_physics.on_mouse_drag,
            self.window_physics.on_mouse_motion,  # Mouse motion handler
            self.window_physics.on_mouse_enter,    # Mouse enter handler
            self.window_physics.on_mouse_leave,    # Mouse leave handler
            on_key_press=self.on_key_press 
        )
        
        pyglet.clock.schedule_interval(self.update, 1/60.0)

    def on_draw(self):
        """
        Main drawing function for this window, called by Pyglet.
        """
        self.window.clear() 
        
        current_camera_distance = self.base_camera_distance * (self.window.height / self.base_window_height)
        if current_camera_distance < 0.1: current_camera_distance = 0.1 

        view = pmath.Mat4.from_translation(pmath.Vec3(0, 0, -current_camera_distance))
        view = view.rotate(self.time * CAMERA_ROTATION_SPEED, pmath.Vec3(0, 1, 0)) 

        aspect_ratio = self.window.width / self.window.height if self.window.height > 0 else 1.0
        projection = pmath.Mat4.perspective_projection(
            aspect_ratio, z_near=0.1, z_far=100.0, fov=CAMERA_FOV
        )
        
        wander_x = math.sin(self.time * WANDER_SPEED) * WANDER_AMPLITUDE
        wander_y = math.cos(self.time * WANDER_SPEED * 0.7 + math.pi/4) * WANDER_AMPLITUDE * 0.6
        wander_z = math.sin(self.time * WANDER_SPEED * 0.5 + math.pi/2) * WANDER_AMPLITUDE * 0.5
        model = pmath.Mat4.from_translation(pmath.Vec3(wander_x, wander_y, wander_z))

        # Draw 3D elements
        if self.aura_visible:
            self.aura_renderer.draw(model, view, projection, self.time, self.bulge_position)
        self.orb_renderer.draw(model, view, projection, self.time, self.bulge_position)
        
        # Draw the batch (contains the name label)
        # Pyglet's batch will handle the 2D rendering automatically
        
        # Update name label position to be centered at the bottom of the window
        # This is done right before drawing to ensure it's in sync with the frame
        if hasattr(self.window_physics, 'name_label'):
            self.window_physics.name_label.x = self.window.width // 2
            self.window_physics.name_label.y = 20  # Small offset from bottom
            
        self.batch.draw()

    def on_resize(self, width, height):
        """
        Handles window resize events for this specific window.
        """
        if height == 0: height = 1 
        glViewport(0, 0, width, height)
        return pyglet.event.EVENT_HANDLED

    def on_key_press(self, symbol, modifiers):
        """
        Handles key press events for this specific window.
        """
        if symbol == key.A:  # Toggle aura visibility when 'A' is pressed
            self.aura_visible = not self.aura_visible
            return pyglet.event.EVENT_HANDLED
        if symbol == key.ESCAPE:
            self.window.close()
            return pyglet.event.EVENT_HANDLED 

    def update(self, dt):
        """
        Main update loop for this window, called periodically by Pyglet.
        """
        self.time += dt
        self.window_physics.update(dt) 

        angle = self.time * 0.5 
        self.bulge_position = [
            math.cos(angle) * 0.5,      
            math.sin(angle * 0.7) * 0.35, 
            math.sin(angle) * 0.5       
        ]

    def cleanup(self):
        """
        Cleans up OpenGL resources associated with this orb and aura.
        """
        if hasattr(self, 'orb_renderer'):
            if hasattr(self.orb_renderer, 'vertex_list') and self.orb_renderer.vertex_list:
                self.orb_renderer.vertex_list.delete()
            if hasattr(self.orb_renderer, 'program') and self.orb_renderer.program:
                self.orb_renderer.program.delete()
        
        if hasattr(self, 'aura_renderer'):
            if hasattr(self.aura_renderer, 'vertex_list') and self.aura_renderer.vertex_list:
                self.aura_renderer.vertex_list.delete()
            if hasattr(self.aura_renderer, 'program') and self.aura_renderer.program:
                self.aura_renderer.program.delete()
        
        print(f"Cleaned up resources for window: {self.window.caption if self.window else 'Unknown'}")
