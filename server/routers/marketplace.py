"""Marketplace REST endpoints (issue #17).

list/buy/cancel are durable intents adjudicated at tick boundaries.
REST stays synchronous: each write enqueues the intent
(commit-before-ack, escrow hold for buys in the same transaction),
pumps the tick's intent queue once in-request, and returns the settled
outcome mapped onto the pre-#17 response shapes -- so RemoteStore and
existing clients need no changes. The same endpoints work whether or
not the authoritative tick loop is running.

Idempotency: pass Idempotency-Key to make a retry return the original
intent's outcome instead of double-applying. Without the header each
call gets a fresh nonce.
"""

import json
import logging
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .. import database
from .. import intents
from .. import market
from .. import viewport
from ..market import MAX_ITEM_SIZE, MAX_JSON_DEPTH, MarketRefusal
from ..models import BuyRequest, MarketListing
from ..rate_limit import market_write_limit, read_limit
from ..security import UserIdentity, get_api_key
from ..sim_gateway import SimError, SimRefusal, SimUnreachable, gateway_for
from .actor import rest_actor

logger = logging.getLogger("soulscape_hub")

router = APIRouter(
    prefix="/marketplace",
    tags=["Marketplace"],
    dependencies=[Depends(get_api_key)],
)

CACHE_TTL = 10.0

_marketplace_cache: dict = {"timestamp": 0.0, "data": None}


def _safe_json_loads(data: str) -> dict:
    if len(data) > MAX_ITEM_SIZE:
        raise HTTPException(status_code=400, detail="Item data too large")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid item JSON")
    _validate_depth(parsed)
    return parsed


def _validate_depth(obj, depth: int = 0):
    if depth > MAX_JSON_DEPTH:
        raise HTTPException(status_code=400, detail="Item nesting too deep")
    if isinstance(obj, dict):
        for v in obj.values():
            _validate_depth(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _validate_depth(v, depth + 1)


def _invalidate_cache() -> None:
    _marketplace_cache["timestamp"] = 0.0
    _marketplace_cache["data"] = None


_REFUSAL_STATUS = {
    "listing_not_found": 404,
    "buyer_not_found": 404,
    "seller_not_found": 400,
    "insufficient_funds": 400,
    "insufficient_inventory": 400,
    "item_unnamed": 400,
    "item_too_large": 400,
    "item_too_deep": 400,
    "custody": 403,
}


def _refusal_http(refusal: MarketRefusal) -> HTTPException:
    return HTTPException(
        status_code=_REFUSAL_STATUS.get(refusal.reason, 400),
        detail=refusal.detail,
    )


def _validate_actor_type(value: str | None, field: str) -> str:
    actor_type = value or database.ACTOR_SOUL
    if actor_type not in (database.ACTOR_SOUL, database.ACTOR_TAMER):
        raise HTTPException(status_code=400, detail=f"Bad {field}")
    return actor_type


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
    soul_id: str,
    custodian_id: str | None,
    payload: dict,
    idempotency_key: str | None,
) -> dict:
    """Enqueue a market intent and settle it within this request."""
    validated, error = intents.validate_payload(kind, payload)
    if error is not None:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {error}")
    session_id = f"rest:{identity.id}"
    nonce = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else "rest_" + secrets.token_urlsafe(16)
    )
    try:
        record = gateway_for(request).submit_intent(
            session_id, nonce, custodian_id, soul_id, kind, validated
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
    if kind == market.KIND_MARKET_BUY:
        if reason == "listing_gone":
            return HTTPException(status_code=404, detail="Listing not found")
        if reason == "custody":
            return HTTPException(status_code=403, detail=detail)
        return HTTPException(status_code=500, detail=detail)
    if kind == market.KIND_MARKET_LIST:
        if reason == "custody":
            return HTTPException(status_code=403, detail=detail)
        return HTTPException(status_code=400, detail=detail)
    if reason == "listing_not_found":
        return HTTPException(status_code=404, detail="Listing not found")
    if reason == "custody":
        return HTTPException(status_code=403, detail=detail)
    return HTTPException(status_code=400, detail=detail)


@router.get("", dependencies=[Depends(read_limit)])
def get_marketplace():
    now = time.time()
    if _marketplace_cache["data"] is not None:
        if now - _marketplace_cache["timestamp"] < CACHE_TTL:
            return _marketplace_cache["data"]
    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM globals WHERE key = 'essence_fund'")
            essence_fund = cursor.fetchone()["value"]

            cursor.execute("SELECT * FROM marketplace")
            listings = []
            for row in cursor.fetchall():
                listing = dict(row)
                listing["item"] = _safe_json_loads(listing["item"])
                listings.append(listing)

            result = {"essence_fund": essence_fund, "listings": listings}
            _marketplace_cache["timestamp"] = now
            _marketplace_cache["data"] = result
            return result
    except Exception as e:
        logger.error(f"Error in get_marketplace: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/list", dependencies=[Depends(market_write_limit)])
def add_listing(
    listing: MarketListing,
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if listing.price <= 0:
        raise HTTPException(status_code=400, detail="Price must be greater than zero")
    seller_type = _validate_actor_type(listing.seller_type, "seller_type")
    # IDOR Mitigation: strictly derive the seller from the identity;
    # operators may name any soul or tamer explicitly.
    actor_type, actor_id, custodian_id = rest_actor(
        identity, seller_type, listing.seller_id
    )
    if (
        identity.is_tamer
        and seller_type == database.ACTOR_SOUL
        and not listing.seller_id
    ):
        raise HTTPException(
            status_code=400,
            detail="seller_id is required when seller_type is 'soul'",
        )

    listing_data = listing.model_dump()
    payload = {
        "item": listing_data["item"],
        "price": listing_data["price"],
        "seller_type": actor_type,
        "seller_soul_id": actor_id,
        "seller_name": listing_data["seller_name"],
    }
    if listing.listing_id:
        payload["listing_id"] = listing.listing_id

    try:
        record = _enqueue_and_settle(
            request,
            identity,
            market.KIND_MARKET_LIST,
            actor_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in add_listing: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        if identity.is_operator and identity.id != actor_id:
            database.audit_log(
                identity.id,
                "marketplace_list_override",
                target_type="listing",
                target_id=record["result"]["listing_id"],
                details=f"seller_id={actor_id}",
            )
        _invalidate_cache()
        return {"status": "success", "listing_id": record["result"]["listing_id"]}
    if record["status"] == "pending":
        return {
            "status": "pending",
            "intent_id": record["intent_id"],
            "listing_id": payload.get("listing_id"),
        }
    raise _map_rejection(market.KIND_MARKET_LIST, record["result"] or {})


@router.post("/buy/{listing_id}", dependencies=[Depends(market_write_limit)])
def buy_item(
    listing_id: str,
    buyer_data: BuyRequest,
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    # IDOR Mitigation: derive the buyer from the identity; operators
    # may name any soul or tamer explicitly.
    buyer_type = _validate_actor_type(buyer_data.buyer_type, "buyer_type")
    actor_type, actor_id, custodian_id = rest_actor(
        identity, buyer_type, buyer_data.buyer_id
    )
    if (
        identity.is_tamer
        and buyer_type == database.ACTOR_SOUL
        and not buyer_data.buyer_id
    ):
        raise HTTPException(
            status_code=400,
            detail="buyer_id is required when buyer_type is 'soul'",
        )

    payload = {
        "listing_id": listing_id,
        "buyer_type": actor_type,
        "buyer_soul_id": actor_id,
    }
    try:
        record = _enqueue_and_settle(
            request,
            identity,
            market.KIND_MARKET_BUY,
            actor_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in buy_item: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        result = record["result"]
        _invalidate_cache()
        if actor_type == database.ACTOR_SOUL:
            viewport.viewport.notify_economy_soul(actor_id)
        seller_type = result.get("seller_type", database.ACTOR_SOUL)
        if seller_type == database.ACTOR_SOUL:
            viewport.viewport.notify_economy_soul(result["seller_id"])
        return {
            "status": "success",
            "item": result["item"],
            "seller_id": result["seller_id"],
            "seller_credited": result["seller_credited"],
            "tax_collected": result["tax_collected"],
        }
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    raise _map_rejection(market.KIND_MARKET_BUY, record["result"] or {})


@router.post("/cancel/{listing_id}", dependencies=[Depends(market_write_limit)])
def cancel_listing(
    listing_id: str,
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    payload = {"listing_id": listing_id}
    actor_type, actor_id, custodian_id = rest_actor(identity)
    payload["actor_type"] = actor_type
    try:
        record = _enqueue_and_settle(
            request,
            identity,
            market.KIND_MARKET_CANCEL,
            actor_id,
            custodian_id,
            payload,
            idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in cancel_listing: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if record["status"] == "adjudicated":
        if identity.is_operator:
            database.audit_log(
                identity.id,
                "marketplace_cancel",
                target_type="listing",
                target_id=listing_id,
                details=f"seller_id={record['result'].get('seller_id')}",
            )
        _invalidate_cache()
        return {
            "status": "success",
            "listing_id": listing_id,
            "seller_id": record["result"].get("seller_id"),
        }
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    raise _map_rejection(market.KIND_MARKET_CANCEL, record["result"] or {})
