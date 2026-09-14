"""
API endpoints for the marketplace functionality, including item listings and trading.
"""

import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from .. import database
from ..models import BuyRequest, MarketListing
from ..security import UserIdentity, get_api_key

logger = logging.getLogger("soulscape_hub")

router = APIRouter(
    prefix="/marketplace",
    tags=["Marketplace"],
    dependencies=[Depends(get_api_key)],
)

MAX_ITEM_SIZE = 100_000
MAX_JSON_DEPTH = 10
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


@router.get("")
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


@router.post("/list")
def add_listing(listing: MarketListing, identity: UserIdentity = Depends(get_api_key)):
    listing_id = listing.listing_id or str(uuid.uuid4())[:8]
    if listing.price <= 0:
        raise HTTPException(status_code=400, detail="Price must be greater than zero")

    listing_data = listing.model_dump()
    # IDOR Mitigation: Strictly derive seller_id from identity
    seller_id = identity.id
    if identity.is_operator and listing.seller_id:
        seller_id = listing.seller_id

    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO marketplace (listing_id, seller_id, seller_name, item, price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    listing_id,
                    seller_id,
                    listing_data["seller_name"],
                    json.dumps(listing_data["item"]),
                    listing_data["price"],
                    listing_data["timestamp"],
                ),
            )
            if identity.is_operator and identity.id != seller_id:
                database.log_audit(
                    cursor, identity.id, "marketplace_list_override",
                    target_type="listing", target_id=listing_id,
                    details=f"seller_id={seller_id}",
                )
            if identity.is_operator and listing.seller_id:
                database.log_audit(
                    cursor, identity.id, "add_listing_as_operator",
                    target_type="listing", target_id=listing_id,
                )
            conn.commit()
            _marketplace_cache["timestamp"] = 0.0
    except Exception as e:
        logger.error(f"Error in add_listing: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "success", "listing_id": listing_id}


@router.post("/buy/{listing_id}")
def buy_item(
    listing_id: str,
    buyer_data: BuyRequest,
    identity: UserIdentity = Depends(get_api_key),
):
    try:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM marketplace WHERE listing_id = ?", (listing_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Listing not found")

            listing = dict(row)
            price = listing["price"]
            seller_id = listing["seller_id"]

            # IDOR Mitigation: Derive buyer_id from identity
            buyer_id = buyer_data.buyer_id
            if identity.is_user:
                buyer_id = identity.id

            # Calculate tax and net
            tax = round(price * 0.02, 2)
            seller_net = round(price - tax, 2)

            # Atomic removal of listing to prevent race conditions
            cursor.execute(
                "DELETE FROM marketplace WHERE listing_id = ?", (listing_id,)
            )
            if cursor.rowcount == 0:
                raise HTTPException(
                    status_code=404, detail="Listing already sold or removed"
                )

            # Atomic deduction from buyer using shared utility
            if not identity.is_operator:
                database.charge_soul(
                    cursor, buyer_id, price, f"purchase of listing {listing_id}"
                )

            # Credit seller
            cursor.execute(
                "UPDATE souls SET essence = essence + ? WHERE soul_id = ?",
                (seller_net, seller_id),
            )
            # Update hub fund
            cursor.execute(
                "UPDATE globals SET value = value + ? WHERE key = 'essence_fund'",
                (tax,),
            )
            conn.commit()
            _marketplace_cache["timestamp"] = 0.0

        return {
            "status": "success",
            "item": _safe_json_loads(listing["item"]),
            "seller_id": seller_id,
            "seller_credited": seller_net,
            "tax_collected": tax,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in buy_item: {e}")
        raise HTTPException(status_code=500, detail=str(e))
