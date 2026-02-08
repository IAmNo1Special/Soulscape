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
from typing import Any

from soulscape.system.logger import log


@dataclass
class Message:
    """Represents a message on the board."""

    message_id: str
    author_id: int
    author_name: str
    content: str
    timestamp: float = field(default_factory=time.time)
    parent_id: str | None = None
    replies: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the message to a dictionary."""
        return {
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "content": self.content,
            "timestamp": self.timestamp,
            "parent_id": self.parent_id,
            "replies": [reply.to_dict() for reply in self.replies],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Deserializes a message from a dictionary."""
        msg = cls(
            message_id=data["message_id"],
            author_id=data["author_id"],
            author_name=data["author_name"],
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

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MessageBoard, cls).__new__(cls)
            cls._instance.posts = {}  # type: dict[str, Message]
            cls._instance.db_path = os.path.join(
                os.getcwd(), "data", "messageboard.json"
            )
            cls._instance._load_data()
        return cls._instance

    def _load_data(self):
        """Loads messages from JSON file."""
        if not os.path.exists(self.db_path):
            return

        try:
            with open(self.db_path, "r") as f:
                data = json.load(f)
                for post_data in data:
                    try:
                        post = Message.from_dict(post_data)
                        self.posts[post.message_id] = post
                    except Exception as e:
                        log.error(f"Failed to load post: {e}")
            log.info(f"Loaded {len(self.posts)} threads from message board.")
        except Exception as e:
            log.error(f"Failed to load message board data: {e}")

    def _save_data(self):
        """Saves messages to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            data = [post.to_dict() for post in self.posts.values()]
            with open(self.db_path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            log.error(f"Failed to save message board data: {e}")

    def create_post(
        self, author_id: int, author_name: str, content: str
    ) -> str:
        """Creates a new top-level post."""
        message_id = str(uuid.uuid4())[:8]
        post = Message(
            message_id=message_id,
            author_id=author_id,
            author_name=author_name,
            content=content,
        )
        self.posts[message_id] = post
        self._save_data()
        return message_id

    def create_reply(
        self, author_id: int, author_name: str, parent_id: str, content: str
    ) -> str | None:
        """Creates a reply to an existing post or message."""
        # Find the root thread
        root_post = self._find_thread_root(parent_id)
        if not root_post:
            return None

        reply_id = str(uuid.uuid4())[:8]
        reply = Message(
            message_id=reply_id,
            author_id=author_id,
            author_name=author_name,
            content=content,
            parent_id=parent_id,
        )

        # Append to the correct parent in structure
        parent_msg = self._find_message_in_tree(root_post, parent_id)
        if parent_msg:
            parent_msg.replies.append(reply)
            self._save_data()
            return reply_id
        return None

    def delete_message(self, author_id: int, message_id: str) -> bool:
        """Deletes a message if the author matches."""
        # Check top-level posts first
        if message_id in self.posts:
            if self.posts[message_id].author_id == author_id:
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
        """Helper to find and delete valid reply."""
        for i, reply in enumerate(parent.replies):
            if reply.message_id == target_id:
                if reply.author_id == author_id:
                    parent.replies.pop(i)
                    return True
                return False
            # Recurse
            if self._delete_recursive(reply, author_id, target_id):
                return True
        return False

    def get_recent_posts(self, limit: int = 10) -> list[Message]:
        """Returns the most recent top-level threads."""
        # Sort by timestamp descending
        sorted_posts = sorted(
            self.posts.values(), key=lambda x: x.timestamp, reverse=True
        )
        return sorted_posts[:limit]

    def _find_thread_root(self, target_id: str) -> Message | None:
        """Finds the top-level post containing the target_id."""
        if target_id in self.posts:
            return self.posts[target_id]

        for post in self.posts.values():
            if self._find_message_in_tree(post, target_id):
                return post
        return None

    def _find_message_in_tree(
        self, root: Message, target_id: str
    ) -> Message | None:
        """Recursive search for a message ID within a thread."""
        if root.message_id == target_id:
            return root
        for reply in root.replies:
            found = self._find_message_in_tree(reply, target_id)
            if found:
                return found
        return None
