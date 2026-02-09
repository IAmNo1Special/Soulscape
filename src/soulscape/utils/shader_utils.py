"""Shader utility classes and functions for hot-reloading."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Callable, Union

import pyglet

from soulscape.system.logger import log
from soulscape.utils.shader_watcher import shader_watcher

# Ensure the shader watcher is properly cleaned up on exit
atexit.register(shader_watcher.stop)


class ShaderProgram:
    """Wrapper around Pyglet's shader program that supports hot-reloading.

    Attributes:
        vertex_shader_path: Absolute path to the vertex shader.
        fragment_shader_path: Absolute path to the fragment shader.
        program: The underlying Pyglet shader program.
        on_reload_callbacks: Set of callbacks to execute after reloading.
    """

    def __init__(
        self,
        vertex_shader_path: Union[str, Path],
        fragment_shader_path: Union[str, Path],
    ) -> None:
        """Initializes the ShaderProgram.

        Args:
            vertex_shader_path: Path to the vertex shader.
            fragment_shader_path: Path to the fragment shader.
        """
        self.vertex_shader_path = Path(vertex_shader_path).resolve()
        self.fragment_shader_path = Path(fragment_shader_path).resolve()
        self.program: pyglet.graphics.shader.ShaderProgram | None = None
        self.on_reload_callbacks: set[Callable[[], None]] = set()
        self.load()
        self._setup_watchers()

    def load(self) -> bool:
        """Load or reload the shader program.

        Returns:
            True if loading succeeded, False otherwise.
        """
        try:
            # Read shader source files
            with open(self.vertex_shader_path, "r", encoding="utf-8") as f:
                vertex_source = f.read()
            with open(self.fragment_shader_path, "r", encoding="utf-8") as f:
                fragment_source = f.read()

            # Create shader objects
            vertex_shader = pyglet.graphics.shader.Shader(
                vertex_source, "vertex"
            )
            fragment_shader = pyglet.graphics.shader.Shader(
                fragment_source, "fragment"
            )

            # Create new program
            new_program = pyglet.graphics.shader.ShaderProgram(
                vertex_shader, fragment_shader
            )

            # Replace old program if it exists
            if self.program:
                old_program = self.program
                pyglet.clock.schedule_once(
                    lambda dt: old_program.delete(), 0
                )  # Defer deletion

            self.program = new_program
            log.debug(
                "Successfully loaded shader program: %s + %s",
                self.vertex_shader_path,
                self.fragment_shader_path,
            )

            # Call reload callbacks
            for callback in self.on_reload_callbacks:
                try:
                    callback()
                except Exception as e:
                    log.error("Error in shader reload callback: %s", e)

            return True

        except Exception as e:
            log.error("Error loading shader program: %s", e)
            if hasattr(e, "log"):
                log.error("Shader compile error: %s", e.log.decode("utf-8"))
            return False

    def _setup_watchers(self) -> None:
        """Set up file watchers for hot-reloading."""

        def reload_shader() -> None:
            log.debug(
                "Shader file changed, reloading: %s or %s",
                self.vertex_shader_path,
                self.fragment_shader_path,
            )
            # Schedule the reload on the main thread
            pyglet.clock.schedule_once(lambda dt: self._safe_reload(), 0)

        # Watch both shader files
        shader_watcher.watch_file(self.vertex_shader_path, reload_shader)
        shader_watcher.watch_file(self.fragment_shader_path, reload_shader)

    def _safe_reload(self) -> None:
        """Safely reload the shader program on the main thread."""
        try:
            self.load()
        except Exception as e:
            log.error("Error reloading shader: %s", e)
            if hasattr(e, "log"):
                log.error("Shader compile error: %s", e.log.decode("utf-8"))

    def on_reload(self, callback: Callable[[], None]) -> None:
        """Register a callback to be called when the shader is reloaded.

        Args:
            callback: The callback function to register.
        """
        self.on_reload_callbacks.add(callback)

    def __getattr__(self, name):
        # Delegate attribute access to the underlying program
        if self.program is None:
            raise AttributeError(
                f"'{self.__class__.__name__}' has no attribute '{name}'"
            )
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


def create_shader_program(
    vertex_shader_path: Union[str, Path],
    fragment_shader_path: Union[str, Path],
) -> ShaderProgram:
    """
    Creates a shader program that supports hot-reloading.

    Args:
        vertex_shader_path: Path to the vertex shader file
        fragment_shader_path: Path to the fragment shader file

    Returns:
        A ShaderProgram instance that can be used like a regular Pyglet shader program
    """
    return ShaderProgram(vertex_shader_path, fragment_shader_path)
