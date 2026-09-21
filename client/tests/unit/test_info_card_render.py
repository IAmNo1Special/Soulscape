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


if __name__ == "__main__":
    unittest.main()
