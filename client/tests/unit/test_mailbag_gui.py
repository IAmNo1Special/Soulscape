"""GUI tests for the mailbag answer surface (issue #32).

Needs a display: run under ``xvfb-run`` in headless environments.
Skips gracefully when no display is available.

Chain under test:
  tap/click on a mailbag bubble or tray item
    -> GUI command {"type": SHOW_MESSAGE_BOARD, "tab": "mailbag"}
    -> message board opens with the Mailbag tab selected
    -> answering a card POSTs through the injected answer callback
"""

from __future__ import annotations

import queue
import time
import unittest


def _try_window():
    try:
        import ttkbootstrap as ttk
    except Exception as exc:
        raise unittest.SkipTest(f"ttkbootstrap unavailable: {exc}")
    try:
        root = ttk.Window(themename="darkly")
    except Exception as exc:
        raise unittest.SkipTest(f"no display: {exc}")
    root.withdraw()
    return root


def _questions():
    now = time.time()
    return [
        {
            "question_id": "q1",
            "soul_id": "s1",
            "question": "Do you get lonely?",
            "created_at": now - 300,
        },
        {
            "question_id": "q2",
            "soul_id": "s1",
            "question": "What is the sky?",
            "created_at": now - 60,
        },
    ]


class _Harness:
    """Fake Hub backing for the mailbag tab callbacks."""

    def __init__(self):
        self.questions = _questions()
        self.answered: list[tuple[str, str]] = []

    def fetch(self):
        return list(self.questions)

    def answer(self, question_id, text):
        self.answered.append((question_id, text))
        self.questions = [
            q for q in self.questions if q["question_id"] != question_id
        ]
        return {"status": "answered", "question_id": question_id}


class TestMailbagTab(unittest.TestCase):
    def setUp(self):
        self.root = _try_window()
        from client.ui.gui.message_board_gui import (
            MailbagQuestionCard,
            MessageBoardWindow,
        )

        self.MailbagQuestionCard = MailbagQuestionCard
        self.harness = _Harness()
        self.win = MessageBoardWindow(
            self.root,
            on_fetch_mailbag=self.harness.fetch,
            on_answer_mailbag=self.harness.answer,
            initial_tab="mailbag",
        )

    def tearDown(self):
        self.win.destroy()
        self.root.destroy()

    def _cards(self):
        return [
            w
            for w in self.win.mailbag_container.winfo_children()
            if isinstance(w, self.MailbagQuestionCard)
        ]

    def test_mailbag_tab_selected_initially(self):
        selected = self.win.notebook.index(self.win.notebook.select())
        mailbag_idx = self.win.notebook.index(self.win.mailbag_tab)
        self.assertEqual(selected, mailbag_idx)

    def test_feed_tab_still_exists(self):
        tabs = self.win.notebook.tabs()
        self.assertEqual(len(tabs), 2)

    def test_one_card_per_pending_question(self):
        self.assertEqual(len(self._cards()), 2)

    def test_answer_flow_posts_and_removes_card(self):
        """Typing an answer and hitting send posts it (the acceptance
        criterion's client half: card -> answer posts)."""
        card = self._cards()[0]
        card.answer_entry.text.insert("1.0", "Only when you are away.")
        card._send()
        self.assertEqual(
            self.harness.answered, [("q1", "Only when you are away.")]
        )
        self.assertIn("sent", card.status.cget("text").lower())
        # The window refreshes after the Hub confirms; the answered
        # question is gone from the pending list.
        self.win._refresh_mailbag()
        remaining = self._cards()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(
            remaining[0].question["question_id"], "q2"
        )

    def test_empty_answer_is_rejected_locally(self):
        card = self._cards()[0]
        card._send()
        self.assertEqual(self.harness.answered, [])
        self.assertIn("first", card.status.cget("text").lower())

    def test_answer_error_surfaces_message(self):
        def failing(qid, text):
            return {"status": "error", "message": "Hub down"}

        self.win.on_answer_mailbag = failing
        card = self._cards()[0]
        card.answer_entry.text.insert("1.0", "hello?")
        card._send()
        self.assertEqual(card.status.cget("text"), "Hub down")


class TestGuiServiceMailbagTab(unittest.TestCase):
    """The GUI process honors tab="mailbag" on SHOW_MESSAGE_BOARD."""

    def setUp(self):
        self.root = _try_window()
        from client.ui.gui.gui_service import GuiCommand, GuiService

        self.GuiCommand = GuiCommand
        self.svc = GuiService(queue.Queue(), queue.Queue())
        self.svc.root = self.root

    def tearDown(self):
        if (
            self.svc.message_board_window is not None
            and self.svc.message_board_window.winfo_exists()
        ):
            self.svc.message_board_window.destroy()
        self.root.destroy()

    def test_show_message_board_selects_mailbag_tab(self):
        self.svc._show_message_board(
            {"type": self.GuiCommand.SHOW_MESSAGE_BOARD, "tab": "mailbag"}
        )
        win = self.svc.message_board_window
        self.assertIsNotNone(win)
        selected = win.notebook.index(win.notebook.select())
        self.assertEqual(selected, win.notebook.index(win.mailbag_tab))

    def test_second_open_lifts_and_selects_mailbag(self):
        self.svc._show_message_board(
            {"type": self.GuiCommand.SHOW_MESSAGE_BOARD}
        )
        win = self.svc.message_board_window
        win.notebook.select(win.feed_tab)
        self.svc._show_message_board(
            {"type": self.GuiCommand.SHOW_MESSAGE_BOARD, "tab": "mailbag"}
        )
        selected = win.notebook.index(win.notebook.select())
        self.assertEqual(selected, win.notebook.index(win.mailbag_tab))


if __name__ == "__main__":
    unittest.main()
