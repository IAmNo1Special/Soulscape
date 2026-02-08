"""Social Interaction System for Soulscape.

This module defines the MessageBoard class (Singleton) and the Message data structure.
It manages a global, persistent message board for asynchronous communication between souls.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from soulscape.system.logger import log
from soulscape.utils.helpers import get_appdata_dir


@dataclass
class Message:
    """Represents a message on the board."""

    message_id: str
    author_id: int
    author_name: str
    title: str
    content: str
    timestamp: float = field(default_factory=time.time)
    parent_id: str | None = None
    replies: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the message to a dictionary.

        Returns:
            A dictionary containing message details.
        """
        return {
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "title": self.title,
            "content": self.content,
            "timestamp": self.timestamp,
            "parent_id": self.parent_id,
            "replies": [reply.to_dict() for reply in self.replies],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Deserializes a message from a dictionary.

        Args:
            data: Serialization dictionary.

        Returns:
            A new Message instance.
        """
        msg = cls(
            message_id=data["message_id"],
            author_id=data["author_id"],
            author_name=data["author_name"],
            title=data["title"],
            content=data["content"],
            timestamp=data["timestamp"],
            parent_id=data.get("parent_id"),
        )
        msg.replies = [
            cls.from_dict(reply) for reply in data.get("replies", [])
        ]
        return msg


class MessageBoard:
    """Global registry of messages (Singleton pattern)."""

    _instance = None

    def __new__(cls) -> MessageBoard:
        """Creates or returns the singleton instance."""
        if cls._instance is None:
            cls._instance = super(MessageBoard, cls).__new__(cls)
            cls._instance.posts = {}  # type: dict[str, Message]
            cls._instance.db_path = cls.get_messageboard_file()
            cls._instance._load_data()
        return cls._instance

    def get_messageboard_file() -> Path:
        """Get the path to messageboard.json."""
        return get_appdata_dir() / "messageboard.json"

    def _load_data(self) -> None:
        """Loads messages from JSON file if changed."""
        if not os.path.exists(self.db_path):
            return

        try:
            mtime = os.path.getmtime(self.db_path)
            # Only reload if file has changed
            if hasattr(self, "_last_mtime") and mtime == self._last_mtime:
                return

            with open(self.db_path, "r") as f:
                data = json.load(f)
                new_posts = {}
                for post_data in data:
                    try:
                        post = Message.from_dict(post_data)
                        new_posts[post.message_id] = post
                    except Exception as e:
                        log.error(f"Failed to load post: {e}")

                self.posts = new_posts
                self._last_mtime = mtime
                log.info(
                    f"Loaded {len(self.posts)} threads from message board."
                )
        except Exception as e:
            log.error(f"Failed to load message board data: {e}")

    def _save_data(self) -> None:
        """Saves messages to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            data = [post.to_dict() for post in self.posts.values()]
            with open(self.db_path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            log.error(f"Failed to save message board data: {e}")

    def create_post(
        self, author_id: int, author_name: str, title: str, content: str
    ) -> Message:
        """Creates and saves a new root post.

        Args:
            author_id: ID of the author.
            author_name: Name of the author.
            title: Title of the thread.
            content: Content of the message.

        Returns:
            The created Message object.
        """
        message_id = str(uuid.uuid4())[:8]
        post = Message(
            message_id=message_id,
            author_id=author_id,
            author_name=author_name,
            title=title,
            content=content,
        )
        self.posts[message_id] = post
        self._save_data()
        return post

    def create_reply(
        self, author_id: int, author_name: str, parent_id: str, content: str
    ) -> Message | None:
        """Creates a reply to an existing post or message.

        Args:
            author_id: ID of the replying author.
            author_name: Name of the replying author.
            parent_id: ID of the message being replied to.
            content: Content of the reply.

        Returns:
            The created Message object if successful, otherwise None.
        """
        # Find the root thread
        root_post = self._find_thread_root(parent_id)
        if not root_post:
            return None

        reply_id = str(uuid.uuid4())[:8]
        reply = Message(
            message_id=reply_id,
            author_id=author_id,
            author_name=author_name,
            title="",  # Replies don't have titles
            content=content,
            parent_id=parent_id,
        )

        # Append to the correct parent in structure
        parent_msg = self._find_message_in_tree(root_post, parent_id)
        if parent_msg:
            parent_msg.replies.append(reply)
            self._save_data()
            return reply
        return None

    def edit_message(
        self, author_id: int, message_id: str, new_content: str
    ) -> bool:
        """Edits an existing message if the author matches.

        Args:
            author_id: ID of the requester.
            message_id: ID of the message to edit.
            new_content: The new text content.

        Returns:
            True if edited, False otherwise.
        """
        # Check top-level posts
        if message_id in self.posts:
            if (
                self.posts[message_id].author_id == author_id
                or author_id == Operator.ID
            ):
                self.posts[message_id].content = new_content
                self._save_data()
                return True
            return False

        # Recursive search
        for post in self.posts.values():
            if self._edit_recursive(post, author_id, message_id, new_content):
                self._save_data()
                return True
        return False

    def _edit_recursive(
        self, parent: Message, author_id: int, target_id: str, new_content: str
    ) -> bool:
        """Helper to find and edit valid reply."""
        for reply in parent.replies:
            if reply.message_id == target_id:
                if reply.author_id == author_id or author_id == Operator.ID:
                    reply.content = new_content
                    return True
                return False
            if self._edit_recursive(reply, author_id, target_id, new_content):
                return True
        return False

    def delete_message(self, author_id: int, message_id: str) -> bool:
        """Deletes a message if the author matches.

        Args:
            author_id: ID of the requester.
            message_id: ID of the message to delete.

        Returns:
            True if deleted, False otherwise.
        """
        # Check top-level posts first
        if message_id in self.posts:
            if (
                self.posts[message_id].author_id == author_id
                or author_id == Operator.ID
            ):
                del self.posts[message_id]
                self._save_data()
                return True
            return False

        # Check replies (recursive search)
        for post in self.posts.values():
            if self._delete_recursive(post, author_id, message_id):
                self._save_data()
                return True
        return False

    def _delete_recursive(
        self, parent: Message, author_id: int, target_id: str
    ) -> bool:
        """Helper to find and delete valid reply.

        Args:
            parent: The parent message to search within.
            author_id: The ID of the requester who wants to delete.
            target_id: The ID of the message to delete.

        Returns:
            True if deleted, False otherwise.
        """
        for i, reply in enumerate(parent.replies):
            if reply.message_id == target_id:
                if reply.author_id == author_id or author_id == Operator.ID:
                    parent.replies.pop(i)
                    return True
                return False
            # Recurse
            if self._delete_recursive(reply, author_id, target_id):
                return True
        return False

    def get_recent_posts(self, limit: int = 10) -> list[Message]:
        """Returns the most recent top-level threads.

        Args:
            limit: Maximum number of posts to return.

        Returns:
            List of Message objects sorted by timestamp (descending).
        """
        # Sort by timestamp descending
        sorted_posts = sorted(
            self.posts.values(), key=lambda x: x.timestamp, reverse=True
        )
        return sorted_posts[:limit]

    def _find_thread_root(self, target_id: str) -> Message | None:
        """Finds the top-level post containing the target_id.

        Args:
            target_id: Message ID to search for.

        Returns:
            The root Message if found, otherwise None.
        """
        if target_id in self.posts:
            return self.posts[target_id]

        for post in self.posts.values():
            if self._find_message_in_tree(post, target_id):
                return post
        return None

    def _find_message_in_tree(
        self, root: Message, target_id: str
    ) -> Message | None:
        """Recursive search for a message ID within a thread.

        Args:
            root: Root message of the subtree.
            target_id: Message ID to find.

        Returns:
            The Message object if found, otherwise None.
        """
        if root.message_id == target_id:
            return root
        for reply in root.replies:
            found = self._find_message_in_tree(reply, target_id)
            if found:
                return found
        return None


class Operator:
    """Represents the system operator/admin."""

    ID = 999
    NAME = "Operator"

    @staticmethod
    def post(title: str, content: str) -> None:
        """Creates a new post as the Operator.

        Args:
            title: The thread title.
            content: The message content.
        """
        MessageBoard().create_post(Operator.ID, Operator.NAME, title, content)

    @staticmethod
    def reply(parent_id: str, content: str) -> None:
        """Creates a reply as the Operator.

        Args:
            parent_id: The ID of the message being replied to.
            content: The reply content.
        """
        MessageBoard().create_reply(
            parent_id, Operator.ID, Operator.NAME, content
        )
