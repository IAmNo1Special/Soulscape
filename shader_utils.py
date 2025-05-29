# shader_utils.py
import pyglet
from pyglet.gl import *
import ctypes
import os
from typing import Optional, Callable

# Import the shader watcher
from shader_watcher import shader_watcher

class ShaderProgram:
    """Wrapper around Pyglet's shader program that supports hot-reloading"""
    def __init__(self, vertex_shader_path: str, fragment_shader_path: str):
        self.vertex_shader_path = os.path.abspath(vertex_shader_path)
        self.fragment_shader_path = os.path.abspath(fragment_shader_path)
        self.program: Optional[pyglet.graphics.shader.ShaderProgram] = None
        self.on_reload_callbacks = set()
        self.load()
        self._setup_watchers()
    
    def load(self) -> bool:
        """Load or reload the shader program"""
        try:
            # Read shader source files
            with open(self.vertex_shader_path, 'r') as f:
                vertex_source = f.read()
            with open(self.fragment_shader_path, 'r') as f:
                fragment_source = f.read()
            
            # Create shader objects
            vertex_shader = pyglet.graphics.shader.Shader(vertex_source, 'vertex')
            fragment_shader = pyglet.graphics.shader.Shader(fragment_source, 'fragment')
            
            # Create new program
            new_program = pyglet.graphics.shader.ShaderProgram(vertex_shader, fragment_shader)
            
            # Replace old program if it exists
            if self.program:
                old_program = self.program
                pyglet.clock.schedule_once(lambda dt: old_program.delete(), 0)  # Defer deletion
            
            self.program = new_program
            print(f"Successfully loaded shader program: {self.vertex_shader_path} + {self.fragment_shader_path}")
            
            # Call reload callbacks
            for callback in self.on_reload_callbacks:
                try:
                    callback()
                except Exception as e:
                    print(f"Error in shader reload callback: {e}")
                    
            return True
            
        except Exception as e:
            print(f"Error loading shader program: {e}")
            if hasattr(e, 'log'):
                print(f"Shader compile error: {e.log.decode('utf-8')}")
            return False
    
    def _setup_watchers(self):
        """Set up file watchers for hot-reloading"""
        def reload_shader():
            print(f"Shader file changed, reloading: {self.vertex_shader_path} or {self.fragment_shader_path}")
            # Schedule the reload on the main thread
            pyglet.clock.schedule_once(lambda dt: self._safe_reload(), 0)
            
        # Watch both shader files
        shader_watcher.watch_file(self.vertex_shader_path, reload_shader)
        shader_watcher.watch_file(self.fragment_shader_path, reload_shader)
    
    def _safe_reload(self):
        """Safely reload the shader program on the main thread"""
        try:
            self.load()
        except Exception as e:
            print(f"Error reloading shader: {e}")
            if hasattr(e, 'log'):
                print(f"Shader compile error: {e.log.decode('utf-8')}")
    
    def on_reload(self, callback: Callable[[], None]):
        """Register a callback to be called when the shader is reloaded"""
        self.on_reload_callbacks.add(callback)
    
    def __getattr__(self, name):
        # Delegate attribute access to the underlying program
        if self.program is None:
            raise AttributeError(f"'{self.__class__.__name__}' has no attribute '{name}'")
        return getattr(self.program, name)
    
    def __enter__(self):
        # For context manager support
        if self.program is None:
            raise RuntimeError("Shader program not loaded")
        return self.program.__enter__()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # For context manager support
        if self.program is not None:
            return self.program.__exit__(exc_type, exc_val, exc_tb)
        return False
    
    def __setitem__(self, key, value):
        # Support dictionary-style assignment for uniforms
        if self.program is None:
            raise RuntimeError("Shader program not loaded")
        self.program[key] = value
    
    def __getitem__(self, key):
        # Support dictionary-style access for uniforms
        if self.program is None:
            raise RuntimeError("Shader program not loaded")
        return self.program[key]

def create_shader_program(vertex_shader_path: str, fragment_shader_path: str) -> ShaderProgram:
    """
    Creates a shader program that supports hot-reloading.
    
    Args:
        vertex_shader_path: Path to the vertex shader file
        fragment_shader_path: Path to the fragment shader file
        
    Returns:
        A ShaderProgram instance that can be used like a regular Pyglet shader program
    """
    return ShaderProgram(vertex_shader_path, fragment_shader_path)

# Ensure the shader watcher is properly cleaned up on exit
import atexit
atexit.register(shader_watcher.stop)
