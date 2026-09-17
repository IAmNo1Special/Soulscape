"""Plot REST endpoints (issue #19).

Claiming is a durable paid intent adjudicated at tick boundaries. REST
stays synchronous: POST /plots/claim enqueues the plot_claim intent
(commit-before-ack, estimated-fee escrow hold in the same transaction),
pumps the tick's intent queue once in-request, and returns the settled
outcome.

Idempotency: pass Idempotency-Key to make a retry return the original
intent's outcome instead of double-claiming. Without the header each
call gets a fresh nonce.
"""

import logging
import secrets
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .. import database
from .. import intents
from .. import plots as plots_lib
from ..rate_limit import market_write_limit, read_limit
from ..security import UserIdentity, get_api_key
from ..sim_gateway import (
    SimCommandError,
    SimError,
    SimRefusal,
    SimUnreachable,
    gateway_for,
)

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/plots", tags=["Plots"], dependencies=[Depends(get_api_key)])

_REFUSAL_STATUS = {
    "claimant_not_found": 404,
    "no_plots_left": 409,
    "plot_taken": 409,
    "custody": 403,
    "insufficient_funds": 400,
    "escrow_missing": 400,
    "bad_payload": 400,
}


def _refusal_http(refusal: plots_lib.PlotRefusal) -> HTTPException:
    return HTTPException(
        status_code=_REFUSAL_STATUS.get(refusal.reason, 400),
        detail=refusal.detail,
    )


def _claim_actor(identity: UserIdentity, soul_id: str | None) -> tuple[str, str | None]:
    """Return (claimant_soul_id, custodian_id) for a REST claim.

    The claimant is always a soul: its wallet pays the claim fee.
    Operators name any soul; tamers name a soul in their custody; soul
    identities always claim for themselves.
    """
    if identity.is_operator:
        if not soul_id:
            raise HTTPException(status_code=400, detail="Operator must specify soul_id")
        return soul_id, None
    if identity.is_tamer:
        if not soul_id:
            raise HTTPException(
                status_code=400,
                detail="Tamer must specify a soul_id to claim for",
            )
        return soul_id, identity.custodian_id
    return identity.id, identity.custodian_id or identity.owner_id


def _sim_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Simulation unavailable: intent not accepted; retry with "
        "the same Idempotency-Key",
    )


def _settle_intent(request: Request, record: dict) -> dict:
    """Wait for the sim's tick to adjudicate; return the fresh intent row."""
    return gateway_for(request).await_settled(record["session_id"], record["nonce"])


@router.get("", dependencies=[Depends(read_limit)])
def list_plots() -> List[Dict[str, Any]]:
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT plot_id, grid_x, grid_y, ring, kind, owner_type, owner_id, "
            "access_policy, claimed_at, claim_seq FROM plots "
            "ORDER BY ring ASC, grid_x ASC, grid_y ASC"
        ).fetchall()
        return [dict(r) for r in rows]


@router.post("/claim", dependencies=[Depends(market_write_limit)])
def claim_plot(
    body: Dict[str, Any],
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    claimant_soul_id, custodian_id = _claim_actor(identity, body.get("soul_id"))
    payload = {
        "claimant_soul_id": claimant_soul_id,
        "access_policy": body.get("access_policy", "open"),
    }
    validated, error = intents.validate_payload(plots_lib.KIND_PLOT_CLAIM, payload)
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
            session_id,
            nonce,
            custodian_id,
            claimant_soul_id,
            plots_lib.KIND_PLOT_CLAIM,
            validated,
        )
    except SimRefusal as refusal:
        raise _refusal_http(refusal)
    except SimUnreachable:
        raise _sim_503()
    except SimError as e:
        logger.error(f"Error in claim_plot: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    record = _settle_intent(request, record)
    if record["status"] == "adjudicated":
        result = record["result"] or {}
        return {
            "status": "success",
            "plot_id": result.get("plot_id"),
            "ring": result.get("ring"),
            "fee": result.get("fee"),
            "claim_seq": result.get("claim_seq"),
            "access_policy": result.get("access_policy"),
        }
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    result = record["result"] or {}
    reason = result.get("reason", "internal")
    detail = result.get("detail", reason)
    raise HTTPException(status_code=_REFUSAL_STATUS.get(reason, 400), detail=detail)


def _may_set_policy(identity: UserIdentity, plot: dict[str, Any]) -> bool:
    if identity.is_operator:
        return True
    if plot.get("owner_type") == "soul" and plot.get("owner_id") == identity.id:
        return True
    if identity.is_tamer and plot.get("owner_id"):
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
                (plot["owner_id"],),
            ).fetchone()
            if (
                row is not None
                and (row["custodian_id"] or row["owner_id"]) == identity.custodian_id
            ):
                return True
    return False


@router.post("/{plot_id}/access", dependencies=[Depends(market_write_limit)])
def set_plot_access(
    plot_id: str,
    body: Dict[str, Any],
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
):
    policy = body.get("access_policy")
    if policy not in plots_lib.ACCESS_POLICIES:
        raise HTTPException(
            status_code=400,
            detail=f"access_policy must be one of {plots_lib.ACCESS_POLICIES}",
        )
    with database.get_db() as conn:
        plot = plots_lib.get_plot(conn, plot_id)
        if plot is None:
            raise HTTPException(status_code=404, detail="Plot not found")
        if plot["kind"] != plots_lib.KIND_CLAIMABLE:
            raise HTTPException(
                status_code=400,
                detail=f"Only claimable plots carry an access policy "
                f"(this one is {plot['kind']})",
            )
        if not _may_set_policy(identity, plot):
            raise HTTPException(
                status_code=403, detail="Only the plot owner may set its policy"
            )
    try:
        return gateway_for(request).command(
            "plot_set_policy",
            {"plot_id": plot_id, "access_policy": policy},
        )
    except SimUnreachable:
        raise HTTPException(status_code=503, detail="Simulation unavailable")
    except SimCommandError as exc:
        logger.error(f"Error in set_plot_access: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
