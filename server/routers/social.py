"""Social REST endpoints (issue #18).

post/reply/edit/delete are durable intents adjudicated at tick
boundaries. REST stays synchronous: each write enqueues the intent
(commit-before-ack, escrow hold for soul-authored posts/replies in the
same transaction), pumps the tick's intent queue once in-request, and
returns the settled outcome mapped onto the pre-#18 response shapes --
so RemoteStore and existing clients need no changes.

Idempotency: pass Idempotency-Key to make a retry return the original
intent's outcome instead of double-applying. Without the header each
call gets a fresh nonce.
"""

import logging
import secrets
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .. import database
from .. import intents
from .. import social as social_lib
from ..models import SocialMessageNode
from ..rate_limit import read_limit, social_write_limit
from ..security import UserIdentity, get_api_key
from ..sim_gateway import SimError, SimRefusal, SimUnreachable, gateway_for
from .actor import rest_actor

logger = logging.getLogger("soulscape_hub")

router = APIRouter(
    prefix="/social", tags=["Social"], dependencies=[Depends(get_api_key)]
)

_REFUSAL_STATUS = {
    "author_not_found": 404,
    "parent_not_found": 404,
    "message_not_found": 404,
    "insufficient_funds": 400,
    "title_required": 400,
    "bad_payload": 400,
    "depth_exceeded": 400,
    "parent_deleted": 400,
    "custody": 403,
}


def _refusal_http(refusal: social_lib.SocialRefusal) -> HTTPException:
    return HTTPException(
        status_code=_REFUSAL_STATUS.get(refusal.reason, 400),
        detail=refusal.detail,
    )


def _sim_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Simulation unavailable: intent not accepted; retry with "
        "the same Idempotency-Key",
    )


def _settle_intent(request: Request, record: dict) -> dict:
    """Wait for the sim's tick to adjudicate; return the fresh intent row."""
    return gateway_for(request).await_settled(
        record["session_id"], record["nonce"]
    )


def _enqueue_and_settle(
    request: Request,
    identity: UserIdentity,
    kind: str,
    author_type: str,
    author_id: str,
    custodian_id: str | None,
    payload: dict,
    idempotency_key: str | None,
) -> dict:
    """Enqueue a social intent and settle it within this request."""
    validated, error = intents.validate_payload(kind, payload)
    if error is not None:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {error}")
    session_id = f"rest:{identity.id}"
    nonce = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else "rest_" + secrets.token_urlsafe(16)
    )
    # The intent's soul is the acting identity, except that an operator
    # creating as a soul names that soul (the wallet charged, per the
    # #17 precedent). Custody of a soul-as-author by a tamer is checked
    # against custodian_id at enqueue and adjudication.
    if identity.is_operator and kind in (
        social_lib.KIND_SOCIAL_POST,
        social_lib.KIND_SOCIAL_REPLY,
    ):
        intent_soul_id = author_id
    else:
        intent_soul_id = identity.id
    try:
        record = gateway_for(request).submit_intent(
            session_id,
            nonce,
            custodian_id,
            intent_soul_id,
            kind,
            validated,
        )
    except SimRefusal as refusal:
        raise _refusal_http(refusal)
    except SimUnreachable:
        raise _sim_503()
    except SimError as exc:
        logger.error(f"Error in _enqueue_and_settle: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    return _settle_intent(request, record)


def _map_rejection(kind: str, result: dict) -> HTTPException:
    reason = result.get("reason", "internal")
    detail = result.get("detail", reason)
    if kind in (social_lib.KIND_SOCIAL_EDIT, social_lib.KIND_SOCIAL_DELETE):
        # Pre-#18 contract: unauthorized or missing edits/deletes are 404.
        return HTTPException(
            status_code=404, detail="Message not found or unauthorized"
        )
    if kind == social_lib.KIND_SOCIAL_REPLY and reason == "parent_not_found":
        return HTTPException(status_code=404, detail=detail)
    if reason == "custody":
        return HTTPException(status_code=403, detail=detail)
    if reason in ("author_not_found", "parent_not_found"):
        return HTTPException(status_code=404, detail=detail)
    return HTTPException(status_code=400, detail=detail)


@router.get(
    "", response_model=List[SocialMessageNode], dependencies=[Depends(read_limit)]
)
def get_social():
    try:
        with database.get_db() as conn:
            return social_lib.build_tree(conn)
    except Exception as e:
        logger.error(f"Error in get_social: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/post", dependencies=[Depends(social_write_limit)])
def create_post(
    post: Dict[str, Any],
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    author_type, author_id, custodian_id = rest_actor(
        identity,
        post.get("author_type"),
        post.get("author_id"),
        operator_must_name=True,
    )
    payload = {
        "title": post.get("title", ""),
        "body": post.get("content", ""),
        "author_type": author_type,
        "author_id": author_id,
        "author_name": post.get("author_name", ""),
    }
    try:
        record = _enqueue_and_settle(
            request,
            identity,
            social_lib.KIND_SOCIAL_POST,
            author_type,
            author_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_post: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        return {
            "status": "success",
            "message_id": record["result"]["message_id"],
            "cost": record["result"]["cost"],
        }
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    raise _map_rejection(social_lib.KIND_SOCIAL_POST, record["result"] or {})


@router.post("/reply", dependencies=[Depends(social_write_limit)])
def reply_to_post(
    reply_data: Dict[str, Any],
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    parent_id = reply_data.get("message_id")
    author_type, author_id, custodian_id = rest_actor(
        identity,
        reply_data.get("author_type"),
        reply_data.get("author_id"),
        operator_must_name=True,
    )
    payload = {
        "parent_id": parent_id,
        "body": reply_data.get("content", ""),
        "author_type": author_type,
        "author_id": author_id,
        "author_name": reply_data.get("author_name", ""),
    }
    try:
        record = _enqueue_and_settle(
            request,
            identity,
            social_lib.KIND_SOCIAL_REPLY,
            author_type,
            author_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in reply_to_post: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        return {
            "status": "success",
            "reply_id": record["result"]["message_id"],
            "cost": record["result"]["cost"],
        }
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    raise _map_rejection(social_lib.KIND_SOCIAL_REPLY, record["result"] or {})


@router.post("/edit/{message_id}", dependencies=[Depends(social_write_limit)])
def edit_message(
    message_id: str,
    edit: Dict[str, Any],
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    author_type, author_id, custodian_id = rest_actor(identity)
    payload = {"message_id": message_id, "body": edit.get("content", "")}
    try:
        record = _enqueue_and_settle(
            request,
            identity,
            social_lib.KIND_SOCIAL_EDIT,
            author_type,
            author_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in edit_message: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        return {"status": "success"}
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    raise _map_rejection(social_lib.KIND_SOCIAL_EDIT, record["result"] or {})


@router.post("/delete/{message_id}", dependencies=[Depends(social_write_limit)])
def delete_message(
    message_id: str,
    payload_body: Dict[str, Any],
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    author_type, author_id, custodian_id = rest_actor(identity)
    payload = {"message_id": message_id}
    try:
        record = _enqueue_and_settle(
            request,
            identity,
            social_lib.KIND_SOCIAL_DELETE,
            author_type,
            author_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in delete_message: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        return {"status": "success"}
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    raise _map_rejection(social_lib.KIND_SOCIAL_DELETE, record["result"] or {})
