"""Metering dispute + operator endpoints (issue #27).

GET /metering/souls/{soul_id}/summary    per-soul spend totals
GET /metering/souls/{soul_id}/line-items each spend, walked end to end:
                                         usage event -> decision trace
                                         (rationale, intents, outcomes)
                                         -> ledger debit rows
GET /metering/pricing                    current pricing knob
POST /metering/pricing                   operator pricing update
                                         (new settlements only)
POST /metering/settle                    operator manual batch trigger

Reads are custodian-scoped (a soul's custodian or the operator), the
same IDOR pattern as the souls endpoints (#8/#9). Pricing writes and
manual settlement are operator-only and audit-logged.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import database
from ..agents import metering
from ..models import PricingUpdate
from ..rate_limit import read_limit
from ..security import UserIdentity, get_api_key, require_scoped
from ..sim_gateway import SimCommandError, SimUnreachable, gateway_for

logger = logging.getLogger("soulscape_hub")

router = APIRouter(
    prefix="/metering", tags=["Metering"], dependencies=[Depends(get_api_key)]
)


def _assert_custody(identity: UserIdentity, soul_id: str) -> None:
    """403 unless the caller is the operator or the soul's custodian."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(custodian_id, owner_id) AS custodian "
            "FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="soul not found")
    if not identity.is_operator and identity.custodian_id != row["custodian"]:
        raise HTTPException(status_code=403, detail="Cross-custody access denied")


def _assert_operator(identity: UserIdentity) -> None:
    if not identity.is_operator:
        raise HTTPException(status_code=403, detail="operator only")


@router.get("/souls/{soul_id}/summary", dependencies=[Depends(read_limit)])
def soul_summary(
    soul_id: str,
    identity: UserIdentity = Depends(require_scoped),
):
    """Per-soul metering totals: calls, tokens, USD estimate, essence
    charged, and unsettled accrued essence at the current pricing."""
    _assert_custody(identity, soul_id)
    with database.get_db() as conn:
        return metering.soul_summary(conn, soul_id)


@router.get("/souls/{soul_id}/line-items", dependencies=[Depends(read_limit)])
def soul_line_items(
    soul_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    identity: UserIdentity = Depends(require_scoped),
):
    """One line per LLM call: usage event -> decision trace
    (rationale, intents, outcomes) -> ledger debit rows."""
    _assert_custody(identity, soul_id)
    with database.get_db() as conn:
        lines = metering.soul_line_items(conn, soul_id, limit=limit)
        conn.commit()
        return lines


@router.get("/pricing", dependencies=[Depends(read_limit)])
def get_pricing(identity: UserIdentity = Depends(get_api_key)):
    """Current pricing knob: per-model USD/1k-token rates and the
    essence_per_usd conversion."""
    with database.get_db() as conn:
        return metering.get_pricing(conn)


@router.post("/pricing")
def update_pricing(
    body: PricingUpdate,
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
):
    """Operator pricing update. Affects NEW settlements only;
    already-settled events keep their recorded essence_charged."""
    _assert_operator(identity)
    try:
        pricing = gateway_for(request).command(
            "metering_set_pricing",
            {
                "essence_per_usd": body.essence_per_usd,
                "model_rates": body.model_rates,
                "operator_id": identity.id,
            },
        )
    except SimUnreachable:
        raise HTTPException(status_code=503, detail="Simulation unavailable")
    except SimCommandError as exc:
        if exc.reason == "bad_request":
            raise HTTPException(status_code=400, detail=exc.detail)
        raise HTTPException(status_code=500, detail=str(exc))
    database.audit_log(
        identity.id,
        "metering_pricing_update",
        target_type="metering",
        target_id="pricing",
        details=(
            f"essence_per_usd={body.essence_per_usd} "
            f"model_rates={'set' if body.model_rates else 'unchanged'}"
        ),
    )
    logger.info("metering: pricing updated by %s", identity.id)
    return pricing


@router.post("/settle")
def trigger_settle(request: Request, identity: UserIdentity = Depends(get_api_key)):
    """Operator manual batch trigger: claim unsettled usage into
    metering_debit intents for the next tick-pump settlement."""
    _assert_operator(identity)
    try:
        report = gateway_for(request).command(
            "metering_settle", {"operator_id": identity.id}
        )
    except SimUnreachable:
        raise HTTPException(status_code=503, detail="Simulation unavailable")
    except SimCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    database.audit_log(
        identity.id,
        "metering_settle",
        target_type="metering",
        target_id=report.get("batch_id") or "",
        details=f"created={report.get('created')}",
    )
    return report
