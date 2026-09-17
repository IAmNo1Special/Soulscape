"""
Tamer account endpoints: register, login, logout with argon2id-hashed
passwords and opaque hashed revocable sessions.
"""

import logging
import re
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status

from .. import database
from ..models import (
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
    try:
        database.create_tamer(tamer_id, username, hash_password(payload.password))
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
