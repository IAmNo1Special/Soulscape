# shader_utils.py
import pyglet
from pyglet.gl import *
import ctypes # Required for GLint and error logging

def create_shader_program(vertex_shader_path, fragment_shader_path):
    """
    Creates and compiles a shader program from vertex and fragment shader files.

    Args:
        vertex_shader_path (str): The file path to the vertex shader source code.
        fragment_shader_path (str): The file path to the fragment shader source code.

    Returns:
        pyglet.graphics.shader.ShaderProgram: A compiled Pyglet shader program.
    """
    try:
        # Read the vertex shader source code
        with open(vertex_shader_path, 'r') as f:
            vertex_source = f.read()
        
        # Read the fragment shader source code
        with open(fragment_shader_path, 'r') as f:
            fragment_source = f.read()

        # Create Pyglet Shader objects. Pyglet handles the compilation internally.
        # This is the correct way to pass shaders to ShaderProgram.
        # Use string literals for shader types as expected by pyglet.graphics.shader.Shader
        vertex_shader = pyglet.graphics.shader.Shader(vertex_source, 'vertex')
        fragment_shader = pyglet.graphics.shader.Shader(fragment_source, 'fragment')
        
        # Create the ShaderProgram by passing the Pyglet Shader objects.
        # Pyglet's ShaderProgram will then handle the linking of these objects.
        return pyglet.graphics.shader.ShaderProgram(vertex_shader, fragment_shader)

    except FileNotFoundError as e:
        print(f"Error: Shader file not found: {e.filename}")
        raise
    except Exception as e:
        # Catch a more general exception for other potential issues during shader creation
        print(f"An unexpected error occurred while creating shader program: {e}")
        # Re-raise the exception to propagate it up for further debugging if needed
        raise

