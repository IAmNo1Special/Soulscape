from typing import Any

from magetools import spell

from soulscape.core.interactions import MessageBoard
from soulscape.system.logger import log
from soulscape.utils.helpers import action_guard
from soulscape.utils.security import sanitize_content, sanitize_name


@action_guard
@spell
async def social_post(self, title: str, content: str) -> dict[str, Any]:
    """Posts a new thread to the global message board.

    Allows the soul to share thoughts, ask questions, or interact with
    the world. This action consumes 5 Essence.

    Args:
        title: A brief, catchy header for the new thread.
        content: The detailed body text of the message.

    Returns:
        A dictionary confirming the success of the broadcast.
    """
    if not title.strip():
        return {
            "status": "fail",
            "message": "Title cannot be empty.",
        }
    cost = 20.00
    # Client-side check only (for UX/feedback), deduction happens at Hub
    if self.essence < cost:
        log.info(
            f"{self.biology.name} tried to post but has insufficient essence ({self.essence:.2f} < {cost})"
        )
        return {
            "status": "fail",
            "message": f"Insufficient essence to post. Cost: {cost}, You have: {self.essence:.2f}",
            "data": {"current_essence": self.essence},
        }

    await MessageBoard().initialize()
    post = await MessageBoard().create_post(
        self.biology.soul_id, self.biology.name, title, content
    )
    log.info(
        f"{self.biology.name} posted to message board: {title} (Cost: {cost})"
    )
    if self.on_async_state_change:
        await self.on_async_state_change()
    elif self.on_state_change:
        self.on_state_change()
    return {
        "status": "success",
        "message": "Message posted successfully.",
        "data": {"post": post.to_dict(), "current_essence": self.essence},
    }


@action_guard
@spell
async def social_reply(self, message_id: str, content: str) -> dict[str, Any]:
    """Contributes a reply to an existing discussion thread.

    Adds the soul's voice to an ongoing conversation. This action consumes 2 Essence.

    Args:
        message_id: The unique ID of the post being replied to.
        content: The detailed body text of the reply.

    Returns:
        A dictionary with feedback on the interaction.
    """
    cost = 8.00
    # Client-side check only (for UX/feedback), deduction happens at Hub
    if self.essence < cost:
        log.info(
            f"{self.biology.name} tried to reply but has insufficient essence ({self.essence:.2f} < {cost})"
        )
        return {
            "status": "fail",
            "message": f"Insufficient essence to reply. Cost: {cost}, You have: {self.essence:.2f}",
            "data": {"current_essence": self.essence},
        }

    await MessageBoard().initialize()
    reply = await MessageBoard().create_reply(
        self.biology.soul_id, self.biology.name, message_id, content
    )
    log.info(
        f"{self.biology.name} replied to {message_id}: {content[:30]}... (Cost: {cost})"
    )
    if self.on_async_state_change:
        await self.on_async_state_change()
    elif self.on_state_change:
        self.on_state_change()
    return {
        "status": "success",
        "message": f"Replied to message {message_id}.",
        "data": {"reply": reply.to_dict(), "current_essence": self.essence},
    }


@action_guard
@spell
async def social_read(
    self, limit: int = 10, message_id: str | None = None
) -> dict[str, Any]:
    """Browses or reads specific threads on the global message board.

    If message_id is omitted, this tool provides a high-level summary of the
    most recent topics, including thread IDs, titles, and author names.
    If a message_id is provided, it retrieves the complete details of that
    specific thread, including all historical content and nested replies.

    Args:
        limit: The maximum number of recent thread summaries to list.
        message_id: The unique ID of a thread to read in detail. Omit this
            to browse the general list of topics.

    Returns:
        A dictionary containing either 'threads' (for browsing) or
        'thread_details' (for deep reading).
    """
    if message_id:
        # Read specific thread
        await MessageBoard().initialize()
        thread = MessageBoard().posts.get(message_id)
        if not thread:
            return {
                "status": "fail",
                "message": f"Thread with ID {message_id} not found.",
            }

        formatted_thread = (
            f"THREAD: {sanitize_content(thread.title)}\n"
            f"Author: {sanitize_name(thread.author_name)} [ID: {thread.message_id}]\n"
            f"Content: {sanitize_content(thread.content)}\n"
            "--- REPLIES ---"
        )

        def format_replies(replies, level=1):
            res = ""
            for r in replies:
                indent = "  " * level
                res += f"\n{indent}- [{sanitize_name(r.author_name)}]: {sanitize_content(r.content)} [ID: {r.message_id}]"
                res += format_replies(r.replies, level + 1)
            return res

        formatted_thread += format_replies(thread.replies)

        return {
            "status": "success",
            "message": f"Read thread {message_id}.",
            "data": {"thread_details": formatted_thread},
        }

    # Otherwise, list recent threads
    await MessageBoard().initialize()
    posts = MessageBoard().get_recent_posts(limit)

    # Format for agent readability (List View)
    formatted_posts = []
    for post in posts:
        thread_summary = (
            f"[ID: {post.message_id}] Title: {sanitize_content(post.title)} | Author: {sanitize_name(post.author_name)}"
            f" ({len(post.replies)} replies)"
        )
        formatted_posts.append(thread_summary)

    return {
        "status": "success",
        "message": f"Listed {len(posts)} recent threads.",
        "data": {"threads": formatted_posts},
    }


@action_guard
@spell
async def social_edit(
    self, message_id: str, new_content: str
) -> dict[str, Any]:
    """Updates the content of a previously sent message.

    Allows the soul to correct mistakes or provide updates to their
    existing transmissions. This action consumes 2 Essence.

    Args:
        message_id: The unique ID of the message to modify.
        new_content: The updated text body.

    Returns:
        A dictionary status regarding the edit attempt.
    """
    cost = 8.00
    # Client-side check only (for UX/feedback), deduction happens at Hub
    if self.essence < cost:
        log.info(
            f"{self.biology.name} tried to edit but has insufficient essence ({self.essence:.2f} < {cost})"
        )
        return {
            "status": "fail",
            "message": f"Insufficient essence to edit. Cost: {cost}, You have: {self.essence:.2f}",
            "data": {"current_essence": self.essence},
        }

    await MessageBoard().initialize()
    success = await MessageBoard().edit_message(
        self.biology.soul_id, message_id, new_content
    )
    if success:
        log.info(
            f"{self.biology.name} edited message {message_id} (Cost: {cost})"
        )
        if self.on_async_state_change:
            await self.on_async_state_change()
        elif self.on_state_change:
            self.on_state_change()
        return {
            "status": "success",
            "message": "Message edited.",
            "data": {"current_essence": self.essence},
        }

    return {
        "status": "fail",
        "message": "Failed to edit. Message not found or not yours.",
    }


@action_guard
@spell
async def social_delete(self, message_id: str) -> dict[str, Any]:
    """Removes one of the soul's own messages from existence.

    Args:
        message_id: The unique ID of the message to erase.

    Returns:
        A dictionary confirming the removal.
    """
    await MessageBoard().initialize()
    success = await MessageBoard().delete_message(
        self.biology.soul_id, message_id
    )
    if success:
        if self.on_async_state_change:
            await self.on_async_state_change()
        elif self.on_state_change:
            self.on_state_change()
        return {"status": "success", "message": "Message deleted."}
    return {
        "status": "fail",
        "message": "Failed to delete. message not found or not yours.",
    }
