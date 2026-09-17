"""Agent Bridge HTTP surface (issue #36).

Token lifecycle (tamer session auth; operators pass an explicit
tamer_id, mirroring the #23 key-vault router):

- POST   /bridge/tokens          create; plaintext returned ONCE
- GET    /bridge/tokens          metadata only, never token material
- DELETE /bridge/tokens/{id}     revoke (revoked tokens -> 401)

Event ingress (integration-token auth, NOT tamer sessions):

- POST /bridge/events            Authorization: Bearer <token>
- GET  /bridge/activity          tamer session auth; the feed behind the
                                 info-card activity log and tray tooltip

All bridge reads are custody-scoped: a tamer sees only their own
tokens and events.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import bridge
from ..models import (
    BridgeActivityItem,
    BridgeEventIn,
    BridgeTokenCreate,
    BridgeTokenCreated,
    BridgeTokenMeta,
)
from ..rate_limit import key_write_limit, read_limit
from ..security import UserIdentity, get_api_key

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/bridge", tags=["Bridge"])


def _resolve_tamer(identity: UserIdentity, tamer_id: str | None) -> str:
    if identity.is_tamer:
        if tamer_id is not None and tamer_id != identity.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tamer may manage only their own bridge tokens",
            )
        return identity.id
    if identity.is_operator:
        if not tamer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Operators must pass tamer_id",
            )
        return tamer_id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Tamer or operator identity required",
    )


def _authenticate_bridge_token(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    scheme, _, presented = auth.partition(" ")
    presented = presented.strip()
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "reason": "missing_token",
                "message": "Authorization: Bearer <integration token> "
                "required",
            },
        )
    row = bridge.resolve_token(presented)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "reason": "invalid_token",
                "message": "Unknown, malformed, or revoked integration "
                "token",
            },
        )
    return row


@router.post(
    "/tokens",
    response_model=BridgeTokenCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(key_write_limit)],
)
def create_bridge_token(
    body: BridgeTokenCreate,
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> BridgeTokenCreated:
    """Create an integration token. The plaintext is returned exactly
    once, in this response; no read path ever returns it again."""
    owner = _resolve_tamer(identity, tamer_id)
    try:
        meta, plaintext = bridge.create_token(owner, body.name)
    except bridge.BridgeRefusal as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": exc.reason, "message": exc.detail},
        ) from exc
    logger.info("Bridge token created: %s for tamer %s",
                meta["token_id"], owner)
    return BridgeTokenCreated(**meta, token=plaintext)


@router.get(
    "/tokens",
    response_model=list[BridgeTokenMeta],
    dependencies=[Depends(read_limit)],
)
def list_bridge_tokens(
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> list[BridgeTokenMeta]:
    """List integration-token metadata. Never returns token material."""
    owner = _resolve_tamer(identity, tamer_id)
    return [BridgeTokenMeta(**row) for row in bridge.list_tokens(owner)]


@router.delete(
    "/tokens/{token_id}", dependencies=[Depends(key_write_limit)]
)
def revoke_bridge_token(
    token_id: str,
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> dict:
    """Revoke an integration token. Idempotent: revoking twice is fine.
    Revoked tokens authenticate as 401 on /bridge/events."""
    owner = _resolve_tamer(identity, tamer_id)
    meta = bridge.revoke_token(owner, token_id)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "message": "No such token"},
        )
    logger.info("Bridge token revoked: %s for tamer %s", token_id, owner)
    return {"status": "revoked", **meta}


_REFUSAL_STATUS = {
    "unknown_kind": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "empty_summary": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "invalid_source_id": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "ref_too_long": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "no_soul": status.HTTP_409_CONFLICT,
}


@router.post("/events")
def post_bridge_event(
    payload: BridgeEventIn, request: Request
) -> dict:
    """Accept one external agent event.

    Auth: ``Authorization: Bearer <integration token>`` (constant-time
    compare; revoked/unknown -> 401). Per-source rate cap:
    60 events/hour/source -> 429. Schema is strict (extra="forbid",
    summary <=280 chars -> 422). Summaries are scrubbed for injection
    attempts -> 422.
    """
    token_row = _authenticate_bridge_token(request)
    retry_after = bridge.check_rate_limit(
        token_row["tamer_id"], payload.source_id
    )
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "reason": "rate_limited",
                "message": f"Per-source cap reached: "
                f"{bridge.RATE_LIMIT_PER_SOURCE} events/hour/source.",
                "retry_after": round(retry_after, 1),
            },
        )
    try:
        return bridge.ingest_event(
            token_row,
            payload.source_id,
            payload.kind,
            payload.summary,
            payload.ref,
            payload.commentary,
        )
    except bridge.InjectionRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "injection_rejected",
                "pattern_class": exc.pattern_class,
                "pattern": exc.pattern,
                "message": "Summary rejected by the injection scrubber.",
            },
        ) from exc
    except bridge.BridgeRefusal as exc:
        raise HTTPException(
            status_code=_REFUSAL_STATUS.get(
                exc.reason, status.HTTP_400_BAD_REQUEST
            ),
            detail={"reason": exc.reason, "message": exc.detail},
        ) from exc


@router.get(
    "/activity",
    response_model=list[BridgeActivityItem],
    dependencies=[Depends(read_limit)],
)
def bridge_activity(
    limit: int = Query(default=30, ge=1, le=100),
    tamer_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
) -> list[BridgeActivityItem]:
    """Recent bridge events for the tamer, newest first. The feed behind
    the info-card activity log and the tray tooltip. Custody-scoped."""
    owner = _resolve_tamer(identity, tamer_id)
    items = []
    for row in bridge.recent_events(owner, limit):
        items.append(
            BridgeActivityItem(
                event_id=row["event_id"],
                soul_id=row["soul_id"],
                source_id=row["source_id"],
                kind=row["kind"],
                summary=row["summary"],
                ref=row.get("ref"),
                created_at=row["created_at"],
            )
        )
    return items
