"""Mailbag endpoints (issue #32): tamer reads + answers soul questions.

- GET /mailbag/count    pending question count (drives the tray badge)
- GET /mailbag/pending  pending questions, oldest first (mailbag tab)
- POST /mailbag/{question_id}/answer  the tamer's answer; custody-checked

Scoping mirrors /souls: non-operators see only their own custody.
Operators see everything. The answer path is custody-checked against
the question's soul (a stranger cannot answer another tamer's souls).
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import database
from .. import mailbag
from ..rate_limit import market_write_limit, read_limit
from ..security import (
    UserIdentity,
    assert_custody,
    get_api_key,
    require_scoped,
)
from ..sim_gateway import SimCommandError, SimUnreachable, gateway_for

router = APIRouter(
    prefix="/mailbag", tags=["Mailbag"], dependencies=[Depends(get_api_key)]
)


class AnswerRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=2000)


def _scope(identity: UserIdentity) -> tuple[str | None, bool, str | None]:
    """(owner/custodian filter, all_souls, soul_id filter).

    Non-operators are scoped to their own custody; role 'user' (a soul
    identity) sees only its own soul's questions. Operators see all.
    """
    if identity.is_operator:
        return None, True, None
    if identity.role == "user":
        return None, False, identity.id
    return identity.custodian_id, False, None


@router.get("/count", dependencies=[Depends(read_limit)])
def mailbag_count(identity: UserIdentity = Depends(require_scoped)):
    """Pending question count for the tray mailbag badge."""
    scope, all_souls, soul_id = _scope(identity)
    with database.get_db() as conn:
        pending = mailbag.pending_for_owner(
            conn, scope or "", all_souls=all_souls, soul_id=soul_id
        )
    return {"pending": len(pending)}


@router.get("/pending", dependencies=[Depends(read_limit)])
def mailbag_pending(identity: UserIdentity = Depends(require_scoped)):
    """Pending questions for the mailbag tab, oldest first."""
    scope, all_souls, soul_id = _scope(identity)
    with database.get_db() as conn:
        questions = mailbag.pending_for_owner(
            conn, scope or "", all_souls=all_souls, soul_id=soul_id
        )
    return {"questions": questions}


@router.post("/{question_id}/answer", dependencies=[Depends(market_write_limit)])
def answer_mailbag(
    question_id: str,
    body: AnswerRequest,
    request: Request,
    identity: UserIdentity = Depends(require_scoped),
):
    """Answer a soul's question (custody-checked).

    The answer is delivered to the soul as a priority observation at
    its next think, and the think is pulled forward (~5 s, same as
    #31's chirp). Returns the delivery summary including latency.
    """
    with database.get_db() as conn:
        row = mailbag.get_question(conn, question_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Question {question_id} not found",
            )
        soul = conn.execute(
            "SELECT soul_id, owner_id, custodian_id FROM souls WHERE soul_id = ?",
            (row["soul_id"],),
        ).fetchone()
    if soul is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Soul not found"
        )
    if identity.role == "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a soul's tamer may answer its questions",
        )
    assert_custody(identity, soul["custodian_id"] or soul["owner_id"])
    try:
        result = gateway_for(request).command(
            "mailbag_answer",
            {
                "question_id": question_id,
                "soul_id": row["soul_id"],
                "answer": body.answer,
                "now": time.time(),
            },
        )
    except SimUnreachable:
        raise HTTPException(status_code=503, detail="Simulation unavailable")
    except SimCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if result["status"] == "refused":
        reason = result["reason"]
        code = status.HTTP_404_NOT_FOUND if reason == "not_found" else 409
        raise HTTPException(
            status_code=code,
            detail={"reason": reason, "message": f"Cannot answer: {reason}."},
        )
    return result
