"""
API endpoints for social interactions, including posts, replies, and moderation.
"""

import logging
import re
import time
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from .. import database
from ..models import MessageDelete, SocialEdit, SocialPost, SocialPostResponse
from ..security import UserIdentity, get_api_key

logger = logging.getLogger("soulscape_hub")

# OPERATOR_ID is imported from security
POST_COST = 20.00
REPLY_COST = 8.00


def _sanitize(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<[^>]*>", "", text)
    clean = clean.replace("[", "&#91;").replace("]", "&#93;")
    clean = clean.replace("{", "&#123;").replace("}", "&#125;")
    return clean[:2000].strip()


router = APIRouter(
    prefix="/social", tags=["Social"], dependencies=[Depends(get_api_key)]
)


@router.get("", response_model=List[SocialPostResponse])
def get_social():
    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM social_posts ORDER BY timestamp DESC")
            posts = {dict(row)["message_id"]: dict(row) for row in cursor.fetchall()}

            for post in posts.values():
                post["replies"] = []

            if not posts:
                return []

            post_ids = list(posts.keys())
            placeholders = ",".join("?" for _ in post_ids)
            cursor.execute(
                f"SELECT *, reply_id AS message_id FROM social_replies WHERE parent_id IN ({placeholders}) ORDER BY timestamp ASC",
                post_ids,
            )

            for row in cursor.fetchall():
                reply = dict(row)
                parent_id = reply.get("parent_id")
                if parent_id in posts:
                    posts[parent_id]["replies"].append(reply)

            return list(posts.values())
    except Exception as e:
        logger.error(f"Error in get_social: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/post")
def create_post(post: SocialPost, identity: UserIdentity = Depends(get_api_key)):
    post_id = post.message_id or str(uuid.uuid4())[:12]
    post_data = post.model_dump()

    # IDOR Mitigation: Strictly derive author_id from identity
    author_id = identity.id
    if identity.is_operator and post.author_id:
        # Operator can override author_id
        author_id = post.author_id

    logger.info(f"Creating post {post_id} from {post_data['author_name']}")
    try:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            # Exempt Operator from charges
            if not identity.is_operator:
                database.charge_soul(cursor, author_id, POST_COST, "create post")

            cursor.execute(
                """
                INSERT INTO social_posts (message_id, author_id, author_name, title, content, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    post_id,
                    author_id,
                    post_data["author_name"],
                    post_data["title"],
                    _sanitize(post_data["content"]),
                    post_data["timestamp"],
                ),
            )
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_post: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "status": "success",
        "message_id": post_id,
        "cost": POST_COST if not identity.is_operator else 0.0,
    }


@router.post("/reply")
def reply_to_post(
    reply_data: Dict[str, Any], identity: UserIdentity = Depends(get_api_key)
):
    message_id = reply_data.get("message_id")
    reply_id = reply_data.get("reply_id") or str(uuid.uuid4())[:12]

    # IDOR Mitigation: Strictly derive author_id from identity
    author_id = identity.id
    if identity.is_operator and reply_data.get("author_id"):
        # Operator can override author_id
        author_id = reply_data.get("author_id")

    try:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            # Exempt Operator from charges
            if not identity.is_operator:
                database.charge_soul(cursor, author_id, REPLY_COST, "post reply")

            # Check if parent exists
            cursor.execute(
                "SELECT 1 FROM social_posts WHERE message_id = ?", (message_id,)
            )
            parent_exists = cursor.fetchone() is not None

            if not parent_exists:
                cursor.execute(
                    "SELECT 1 FROM social_replies WHERE reply_id = ?",
                    (message_id,),
                )
                parent_exists = cursor.fetchone() is not None

            if not parent_exists:
                raise HTTPException(
                    status_code=404,
                    detail=f"Target message {message_id} not found",
                )

            cursor.execute(
                """
                INSERT INTO social_replies (reply_id, parent_id, author_id, author_name, content, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    reply_id,
                    message_id,
                    author_id,
                    reply_data["author_name"],
                    _sanitize(reply_data["content"]),
                    time.time(),
                ),
            )
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in reply_to_post: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "status": "success",
        "reply_id": reply_id,
        "cost": REPLY_COST if not identity.is_operator else 0.0,
    }


@router.post("/edit/{message_id}")
def edit_message(
    message_id: str,
    edit: SocialEdit,
    identity: UserIdentity = Depends(get_api_key),
):
    # IDOR Mitigation: Strictly derive author_id from identity
    author_id = identity.id
    # Note: Operators usually edit as system, if they need to edit others'
    # we would handle that separately, but for now strict ownership.

    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE social_posts SET content = ? WHERE message_id = ? AND author_id = ?",
                (edit.content, message_id, author_id),
            )
            rows_affected = cursor.rowcount
            if rows_affected == 0:
                cursor.execute(
                    "UPDATE social_replies SET content = ? WHERE reply_id = ? AND author_id = ?",
                    (edit.content, message_id, author_id),
                )
                rows_affected = cursor.rowcount

            if rows_affected == 0:
                raise HTTPException(
                    status_code=404, detail="Message not found or unauthorized"
                )
            conn.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in edit_message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete/{message_id}")
def delete_message(
    message_id: str,
    payload: MessageDelete,
    identity: UserIdentity = Depends(get_api_key),
):
    # IDOR Mitigation: Strictly derive author_id from identity
    author_id = identity.id

    rows_affected = 0
    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            # Attempt to delete as author
            cursor.execute(
                "DELETE FROM social_posts WHERE message_id = ? AND author_id = ?",
                (message_id, author_id),
            )
            rows_affected += cursor.rowcount

            if rows_affected == 0:
                cursor.execute(
                    "DELETE FROM social_replies WHERE reply_id = ? AND author_id = ?",
                    (message_id, author_id),
                )
                rows_affected += cursor.rowcount

            # If not deleted, try as operator (system admin)
            if rows_affected == 0 and identity.is_operator:
                database.log_audit(
                    cursor, identity.id, "social_delete_as_operator",
                    target_type="message", target_id=message_id,
                )
                cursor.execute(
                    "DELETE FROM social_posts WHERE message_id = ?",
                    (message_id,),
                )
                rows_affected += cursor.rowcount
                if rows_affected == 0:
                    cursor.execute(
                        "DELETE FROM social_replies WHERE reply_id = ?",
                        (message_id,),
                    )
                    rows_affected += cursor.rowcount

            if rows_affected == 0:
                raise HTTPException(
                    status_code=404,
                    detail="Message not found or unauthorized",
                )
            conn.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in delete_message: {e}")
        raise HTTPException(status_code=500, detail=str(e))
