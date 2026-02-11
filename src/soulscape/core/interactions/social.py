"""Social Interaction System for Soulscape.

This module defines the MessageBoard class (Singleton) and the Message data structure.
It manages a global, persistent message board for asynchronous communication between souls.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from soulscape.system.logger import log

if TYPE_CHECKING:
    from ..stores import DataStore


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
            message_id=data.get("message_id") or data.get("reply_id"),
            author_id=data["author_id"],
            author_name=data["author_name"],
            title=data.get("title", ""),
            content=data["content"],
            timestamp=data.get("timestamp", time.time()),
            parent_id=data.get("parent_id"),
        )
        msg.replies = [
            cls.from_dict(reply) for reply in data.get("replies", [])
        ]
        return msg


class MessageBoard:
    """Global registry of messages (Singleton pattern)."""

    _instance = None

    def __new__(cls, store: DataStore | None = None) -> MessageBoard:
        """Creates or returns the singleton instance."""
        if cls._instance is None:
            cls._instance = super(MessageBoard, cls).__new__(cls)
            cls._instance.posts = {}  # type: dict[str, Message]

            # Default to LocalStore if none provided
            if store is None:
                from ..stores import get_default_store

                store = get_default_store()
            cls._instance.store = store
            # Need to call initialize() to load data
            cls._instance.initialized = False
        return cls._instance

    async def initialize(self) -> None:
        """Asynchronously loads initial data if not already done.

        Use this for the first load. For subsequent forced reloads,
        use refresh().
        """
        if not self.initialized:
            await self._load_data()
            self.initialized = True

    async def refresh(self) -> None:
        """Forces a reload of data from the store."""
        await self._load_data()
        self.initialized = True

    async def _load_data(self) -> None:
        """Loads messages from the data store asynchronously."""
        try:
            data = await self.store.load_messageboard()
            if not data:
                return

            new_posts = {}
            for post_data in data:
                try:
                    post = Message.from_dict(post_data)
                    new_posts[post.message_id] = post
                except Exception as e:
                    log.error(f"Failed to load post: {e}")

            self.posts = new_posts
            log.info(f"Loaded {len(self.posts)} threads from data store.")
        except Exception as e:
            log.error(f"Failed to load message board data from store: {e}")

    async def _save_data(self) -> None:
        """Saves messages to the data store asynchronously."""
        try:
            data = [post.to_dict() for post in self.posts.values()]
            await self.store.save_messageboard(data)
        except Exception as e:
            log.error(f"Failed to save message board data to store: {e}")

    async def create_post(
        self, author_id: int, author_name: str, title: str, content: str
    ) -> Message:
        """Creates and saves a new root post asynchronously."""
        message_id = str(uuid.uuid4())[:12]
        post = Message(
            message_id=message_id,
            author_id=author_id,
            author_name=author_name,
            title=title,
            content=content,
        )
        self.posts[message_id] = post
        # Granular Hub update
        await self.store.add_post(post.to_dict())
        await self._save_data()
        return post

    async def create_reply(
        self, author_id: int, author_name: str, parent_id: str, content: str
    ) -> Message | None:
        """Creates a reply asynchronously."""
        # Find the root thread
        root_post = self._find_thread_root(parent_id)
        if not root_post:
            return None

        reply_id = str(uuid.uuid4())[:12]
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
            # Granular Hub update
            await self.store.add_reply(
                {
                    "message_id": parent_id,
                    "author_id": author_id,
                    "author_name": author_name,
                    "content": content,
                }
            )
            await self._save_data()
            return reply
        return None

    async def edit_message(
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
                # Granular Hub update
                await self.store.edit_post(message_id, new_content, author_id)
                self.posts[message_id].content = new_content
                await self._save_data()
                return True
            return False

        # Recursive search
        for post in self.posts.values():
            if await self._edit_recursive(
                post, author_id, message_id, new_content
            ):
                await self._save_data()
                return True
        return False

    async def _edit_recursive(
        self, parent: Message, author_id: int, target_id: str, new_content: str
    ) -> bool:
        """Helper to find and edit valid reply."""
        for reply in parent.replies:
            if reply.message_id == target_id:
                if reply.author_id == author_id or author_id == Operator.ID:
                    # Granular Hub update
                    await self.store.edit_post(
                        target_id, new_content, author_id
                    )
                    reply.content = new_content
                    return True
                return False
            if await self._edit_recursive(
                reply, author_id, target_id, new_content
            ):
                return True
        return False

    async def delete_message(self, author_id: int, message_id: str) -> bool:
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
                # Granular Hub update
                await self.store.delete_post(message_id, author_id)
                del self.posts[message_id]
                await self._save_data()
                return True
            return False

        # Check replies (recursive search)
        for post in self.posts.values():
            if await self._delete_recursive(post, author_id, message_id):
                await self._save_data()
                return True
        return False

    async def _delete_recursive(
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
                    # Granular Hub update
                    await self.store.delete_post(target_id, author_id)
                    parent.replies.pop(i)
                    return True
                return False
            # Recurse
            if await self._delete_recursive(reply, author_id, target_id):
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
    async def post(title: str, content: str) -> None:
        """Creates a new post as the Operator.

        Args:
            title: The thread title.
            content: The message content.
        """
        await MessageBoard().initialize()
        await MessageBoard().create_post(
            Operator.ID, Operator.NAME, title, content
        )

    @staticmethod
    async def reply(parent_id: str, content: str) -> None:
        """Creates a reply as the Operator.

        Args:
            parent_id: The ID of the message being replied to.
            content: The reply content.
        """
        await MessageBoard().initialize()
        await MessageBoard().create_reply(
            Operator.ID, Operator.NAME, parent_id, content
        )
