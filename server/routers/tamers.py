"""
Tamer account endpoints: register, login, logout with argon2id-hashed
passwords and opaque hashed revocable sessions.
"""

import logging
import re
import secrets
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status

from .. import database
from .. import dormancy
from .. import market
from .. import persistence
from .. import wallets
from ..models import (
    FundSoulRequest,
    TamerLogin,
    TamerRegister,
    TamerResponse,
    TamerSessionResponse,
)
from ..rate_limit import check_login_limit
from ..security import (
    UserIdentity,
    api_key_header_scheme,
    get_api_key,
    hash_password,
    issue_tamer_session,
    revoke_tamer_session,
    verify_password,
)

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/tamers", tags=["Tamers"])

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
TAMER_PASSWORD_MIN_LENGTH = 8


@router.post(
    "/register", response_model=TamerResponse, status_code=status.HTTP_201_CREATED
)
def register_tamer(payload: TamerRegister) -> TamerResponse:
    """Create a tamer account with an argon2id-hashed password."""
    username = payload.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-32 characters: letters, digits, _ or -",
        )
    if len(payload.password) < TAMER_PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Password must be at least {TAMER_PASSWORD_MIN_LENGTH} characters"
            ),
        )
    tamer_id = "tmr_" + secrets.token_urlsafe(16)
    now = time.time()
    try:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO tamers "
                    "(tamer_id, username, password_hash, created_at, essence) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        tamer_id,
                        username,
                        hash_password(payload.password),
                        now,
                        dormancy.STARTER_GRANT,
                    ),
                )
                dormancy.mint_starter_grant(
                    conn,
                    persistence.last_tick_meta(conn)[1],
                    database.ACTOR_TAMER,
                    tamer_id,
                    intent_id=f"register:{tamer_id}",
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )
    logger.info(f"Tamer registered: {tamer_id} ({username})")
    return TamerResponse(tamer_id=tamer_id, username=username)


@router.post("/login", response_model=TamerSessionResponse)
def login_tamer(payload: TamerLogin, request: Request) -> TamerSessionResponse:
    """Verify credentials and issue an opaque revocable session token."""
    username = payload.username.strip()
    ip = request.client.host if request.client else "unknown"
    check_login_limit(username, ip)
    tamer = database.get_tamer_by_username(username)
    if tamer is None or not verify_password(payload.password, tamer["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    token, expires_at = issue_tamer_session(tamer["tamer_id"])
    return TamerSessionResponse(
        token=token,
        tamer_id=tamer["tamer_id"],
        username=tamer["username"],
        expires_at=expires_at,
    )


@router.post("/logout")
def logout_tamer(
    api_key: str | None = Security(api_key_header_scheme),
    identity: UserIdentity = Depends(get_api_key),
) -> dict:
    """Revoke the presenting tamer session server-side."""
    if not identity.is_tamer or not api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tamer session required",
        )
    revoke_tamer_session(api_key)
    return {"status": "ok"}


@router.post("/fund-soul")
def fund_soul(
    payload: FundSoulRequest,
    identity: UserIdentity = Depends(get_api_key),
) -> dict:
    """Gift essence from the tamer's own wallet to a custodied soul.

    One-way: the tamer wallet is debited, the soul wallet credited,
    and both movements are journaled as ledger rows so conservation
    accounting stays exact. The soul's funding refresh is journaled
    for dormancy like any other credit.
    """
    if not identity.is_tamer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tamer session required",
        )
    amount = payload.amount
    if not isinstance(amount, (int, float)) or not amount > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Amount must be positive",
        )
    amount = float(amount)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            soul = conn.execute(
                "SELECT custodian_id, owner_id, "
                "COALESCE(essence, 0.0) AS essence "
                "FROM souls WHERE soul_id = ?",
                (payload.soul_id,),
            ).fetchone()
            if soul is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Soul not found",
                )
            custodian = soul["custodian_id"] or soul["owner_id"]
            if custodian != identity.custodian_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No custody of this soul",
                )
            tamer_before = wallets.cached_balance(
                conn, database.ACTOR_TAMER, identity.id
            )
            if tamer_before is None or not wallets.debit(
                conn, database.ACTOR_TAMER, identity.id, amount
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Insufficient tamer essence",
                )
            soul_before = float(soul["essence"])
            wallets.credit(conn, database.ACTOR_SOUL, payload.soul_id, amount)
            tick_id = persistence.last_tick_meta(conn)[1]
            intent_id = f"fund-soul:{identity.id}:{payload.soul_id}:{now:.6f}"
            conn.execute(
                "INSERT INTO ledger "
                "(tick_id, intent_id, entry_type, actor_type, soul_id, amount, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tick_id,
                    intent_id,
                    market.LEDGER_DEBIT,
                    database.ACTOR_TAMER,
                    identity.id,
                    amount,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO ledger "
                "(tick_id, intent_id, entry_type, actor_type, soul_id, amount, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tick_id,
                    intent_id,
                    market.LEDGER_CREDIT,
                    database.ACTOR_SOUL,
                    payload.soul_id,
                    amount,
                    now,
                ),
            )
            dormancy.note_essence_change(
                conn, payload.soul_id, soul_before, tick_id, now=now
            )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
    return {
        "status": "success",
        "tamer_id": identity.id,
        "soul_id": payload.soul_id,
        "amount": amount,
        "tamer_essence": tamer_before - amount,
        "soul_essence": soul_before + amount,
    }
