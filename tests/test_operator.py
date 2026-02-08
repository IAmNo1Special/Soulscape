import os
import sys
import unittest
from unittest.mock import patch

# Ensure src is in the python path
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src"))
)

from soulscape.core.social import Operator


class TestOperator(unittest.TestCase):
    """Tests for the Operator class."""

    @patch("soulscape.core.social.MessageBoard.create_post")
    def test_operator_post(self, mock_create_post):
        """Verify Operator.post calls MessageBoard.create_post with correct args."""
        content = "Test Message"
        Operator.post(content)

        # Check if called with Operator ID, Name, and Content
        mock_create_post.assert_called_once_with(
            Operator.ID, Operator.NAME, content
        )

    @patch("soulscape.core.social.MessageBoard.create_reply")
    def test_operator_reply(self, mock_create_reply):
        """Verify Operator.reply calls MessageBoard.create_reply with correct args."""
        parent_id = "12345"
        content = "Test Reply"
        Operator.reply(parent_id, content)

        # Check if called with Parent ID, Operator ID, Name, and Content
        # Note: create_reply sig is (parent_id, author_id, author_name, content)
        mock_create_reply.assert_called_once_with(
            parent_id, Operator.ID, Operator.NAME, content
        )


if __name__ == "__main__":
    unittest.main()
    unittest.main()
