from __future__ import annotations

import unittest

import pyglet


class InfoCardRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.window = pyglet.window.Window(width=640, height=480, visible=False)
        cls.window.switch_to()
        from client.ui.graphics.scene_renderer import SceneRenderer

        cls.renderer = SceneRenderer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()

    def test_multi_line_card_renders(self) -> None:
        self.renderer.render_info_card(
            320,
            240,
            ["Soul 1", "needs: content", "activity: idle", "essence: 100"],
            640,
            480,
        )

    def test_single_title_line_renders(self) -> None:
        self.renderer.render_info_card(320, 240, ["Soul 1"], 640, 480)

    def test_empty_lines_are_noop(self) -> None:
        self.renderer.render_info_card(320, 240, [], 640, 480)


class BubbleRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.window = pyglet.window.Window(width=640, height=480, visible=False)
        cls.window.switch_to()
        from client.ui.graphics.scene_renderer import SceneRenderer

        cls.renderer = SceneRenderer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()

    def test_bubble_renders(self) -> None:
        self.renderer.render_bubbles([(320, 240, "Hello!")])

    def test_no_jobs_is_noop(self) -> None:
        self.renderer.render_bubbles([])


class FullViewportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.window = pyglet.window.Window(width=640, height=480, visible=False)
        cls.window.switch_to()
        from client.ui.graphics.scene_renderer import SceneRenderer

        cls.renderer = SceneRenderer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()

    def test_render_restores_full_viewport_for_overlays(self) -> None:
        from pyglet.gl import GL_VIEWPORT, GLint, glGetIntegerv

        from client.core.soul.soul import Soul

        soul = Soul(
            orb_color_rgb=(1.0, 0.0, 0.0),
            aura_color_rgb=(0.0, 1.0, 0.0),
            name="Viewport Soul",
            initial_position=(100, 100),
            screen_width=640,
            screen_height=480,
            owner_id="hub",
            local_instance_id="me",
        )
        try:
            self.renderer.render([soul], 640, 480)
            box = (GLint * 4)()
            glGetIntegerv(GL_VIEWPORT, box)
            self.assertEqual(tuple(box), (0, 0, 640, 480))
        finally:
            soul.cleanup()


if __name__ == "__main__":
    unittest.main()
