"""Tamer presence REST ingress (issue #28).

Tamer-scoped: the authenticated identity IS the tamer -- no client field
names a tamer, so one tamer can never report (or spoof) another's presence.
Operators are rejected outright: an operator identity carries no tamer
scope and must not be able to fabricate tamer presence.

Schema enforcement is pydantic `extra="forbid"` (422 on padded payloads,
unknown enum values). The opt-in gate for `app_category` is checked
against the server-stored toggle: `app_category` without opt-in is 422.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import intents
from .. import presence as presence_module
from ..models import (
    PresenceOptIn,
    TamerPresenceReport,
    TamerPresenceState,
)
from ..security import UserIdentity, require_scoped
from ..world_tick import WorldTick

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/presence", tags=["Presence"])


def _tamer_id_or_403(identity: UserIdentity) -> str:
    tamer_id = identity.custodian_id
    if identity.is_operator or not tamer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Presence is tamer-scoped: operators and unscoped "
            "identities cannot report tamer presence",
        )
    return tamer_id


def _settle_intent(request: Request, record: dict) -> dict:
    tick = getattr(request.app.state, "world_tick", None)
    if tick is None:
        tick = WorldTick()
    tick.pump_intents()
    fresh = intents.get_intent_by_nonce(record["session_id"], record["nonce"])
    assert fresh is not None
    return fresh


@router.post("/report", response_model=TamerPresenceState)
def report_presence(
    payload: TamerPresenceReport,
    request: Request,
    identity: UserIdentity = Depends(require_scoped),
) -> TamerPresenceState:
    """Accept a redacted presence report as a durable intent."""
    tamer_id = _tamer_id_or_403(identity)
    data = payload.model_dump(exclude_none=True)
    if data.get("app_category") is not None and not presence_module.get_app_opt_in(
        tamer_id
    ):
        raise HTTPException(
            status_code=422,  # 422 Unprocessable Content
            detail="app_category requires the tamer's explicit opt-in",
        )
    validated, error = intents.validate_payload("tamer_presence", data)
    if error is not None or validated is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid presence payload: {error}",
        )
    session_id = f"rest:{identity.id}"
    nonce = presence_module.make_nonce()
    try:
        record = presence_module.enqueue_presence_intent(
            session_id, nonce, tamer_id, validated
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    fresh = _settle_intent(request, record)
    result = fresh.get("result") or {}
    if fresh["status"] == "rejected":
        raise HTTPException(
            status_code=422,  # 422 Unprocessable Content
            detail=f"Presence rejected: {result.get('reason', 'unknown')}",
        )
    state = presence_module.get_presence(tamer_id)
    assert state is not None
    return TamerPresenceState(tamer_id=tamer_id, **state)


@router.put("/app-opt-in", response_model=dict)
def set_app_opt_in(
    payload: PresenceOptIn,
    identity: UserIdentity = Depends(require_scoped),
) -> dict:
    """Toggle the opt-in coarse app-category signal for the tamer."""
    tamer_id = _tamer_id_or_403(identity)
    if not presence_module.set_app_opt_in(tamer_id, payload.enabled):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tamer not found"
        )
    logger.info("presence app opt-in: tamer=%s enabled=%s", tamer_id, payload.enabled)
    return {"tamer_id": tamer_id, "app_category_opt_in": payload.enabled}


@router.get("", response_model=TamerPresenceState)
def read_presence(
    identity: UserIdentity = Depends(require_scoped),
) -> TamerPresenceState:
    """Read the tamer's own current presence (staleness applied)."""
    tamer_id = _tamer_id_or_403(identity)
    state = presence_module.get_presence(tamer_id)
    if state is None:
        return TamerPresenceState(tamer_id=tamer_id)
    return TamerPresenceState(tamer_id=tamer_id, **state)
