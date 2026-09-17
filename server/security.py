import hashlib
import os
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Security, WebSocket, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from starlette.status import HTTP_403_FORBIDDEN

from . import database

# Define the API Key header scheme
API_KEY_NAME = "X-Hub-Secret"
# We use auto_error=False so we can return a custom 403 instead of 401
api_key_header_scheme = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

OPERATOR_ID = os.getenv("HUB_OPERATOR_ID", "HUB_OPERATOR")
SECRET_MIN_LENGTH = 16
TOKEN_LIFETIME_SECONDS = 30 * 24 * 3600

TAMER_SESSION_PREFIX = "tms_"
TAMER_SESSION_TTL_SECONDS = 7 * 24 * 3600

WS_TICKET_PREFIX = "wst_"
WS_TICKET_TTL_SECONDS = 60


def hash_password(password: str) -> str:
    return database.hash_secret(password)


def verify_password(password: str, password_hash: str) -> bool:
    return database.verify_secret_hash(password, password_hash)


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_tamer_session(tamer_id: str) -> tuple[str, float]:
    """Create an opaque tamer session. Returns (token, expires_at).
    Only the hash is stored."""
    token = TAMER_SESSION_PREFIX + secrets.token_urlsafe(48)
    expires_at = time.time() + TAMER_SESSION_TTL_SECONDS
    database.create_tamer_session(tamer_id, _hash_session_token(token), expires_at)
    return token, expires_at


def revoke_tamer_session(token: str) -> bool:
    """Revoke a tamer session token server-side."""
    if not token.startswith(TAMER_SESSION_PREFIX):
        return False
    return database.revoke_tamer_session(_hash_session_token(token))


def _tamer_identity_from_token(token: str) -> "UserIdentity | None":
    if not token.startswith(TAMER_SESSION_PREFIX):
        return None
    session = database.get_tamer_session(_hash_session_token(token))
    if session is None:
        return None
    return UserIdentity(
        id=session["tamer_id"],
        custodian_id=session["tamer_id"],
        role="tamer",
    )


def validate_secret(secret: str) -> str:
    if not secret or len(secret) < SECRET_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Secret must be at least {SECRET_MIN_LENGTH} characters",
        )
    return secret


def make_secret_record(secret: str) -> tuple[str, str]:
    """Create (secret_hash, secret_prefix) from a plaintext secret."""
    secret_hash = database.hash_secret(secret)
    secret_prefix = secret_hash[:16]  # First 16 chars of hash for O(1) lookup
    return secret_hash, secret_prefix


def generate_token_expiry() -> float:
    return time.time() + TOKEN_LIFETIME_SECONDS


class UserIdentity(BaseModel):
    id: str  # Soul ID, Tamer ID, or Operator ID
    owner_id: Optional[str] = None
    custodian_id: Optional[str] = None
    role: str

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"

    @property
    def is_user(self) -> bool:
        return self.role in ("user", "tamer")

    @property
    def is_tamer(self) -> bool:
        return self.role == "tamer"


def get_hub_secret():
    """Retrieves the hub secret from environment variables."""
    secret = os.getenv("HUB_SECRET_KEY")
    if not secret:
        # In production, this should probably raise an error or prevent startup
        return None
    return secret


async def get_api_key(
    api_key: str | None = Security(api_key_header_scheme),
) -> UserIdentity:
    """
    Validates the API Key provided in the header.
    Returns a UserIdentity object if valid.
    """
    hub_secret = get_hub_secret()

    if not hub_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfigured: HUB_SECRET_KEY not set",
        )

    if api_key:
        # Check for Operator key
        if secrets.compare_digest(api_key, hub_secret):
            return UserIdentity(id=OPERATOR_ID, role="operator")

        # Check for Tamer session token
        tamer_identity = _tamer_identity_from_token(api_key)
        if tamer_identity is not None:
            return tamer_identity

        # Check for individual Soul secret (hashed lookup)
        prefix = database.hash_secret(api_key)[:16]
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id, owner_id, custodian_id, token_expiry, "
                "is_revoked, secret_hash "
                "FROM souls WHERE secret_prefix = ?",
                (prefix,),
            )
            for row in cursor.fetchall():
                if database.verify_secret_hash(api_key, row["secret_hash"]):
                    if row["is_revoked"]:
                        raise HTTPException(
                            status_code=HTTP_403_FORBIDDEN,
                            detail="Token has been revoked",
                        )
                    if row["token_expiry"] and row["token_expiry"] < time.time():
                        raise HTTPException(
                            status_code=HTTP_403_FORBIDDEN,
                            detail="Token has expired",
                        )
                    return UserIdentity(
                        id=row["soul_id"],
                        owner_id=row["owner_id"],
                        custodian_id=row["custodian_id"] or row["owner_id"],
                        role="user",
                    )

    raise HTTPException(
        status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
    )


async def verify_ws_token(
    websocket: WebSocket,
    token: str | None,
    x_hub_secret: str | None,
) -> UserIdentity | None:
    """
    Validates the API Key provided in the query param or header for WebSockets.
    Returns UserIdentity if valid, else None.
    """
    hub_secret = get_hub_secret()

    if not hub_secret:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None

    received_token = token or x_hub_secret

    if received_token:
        # Check for Operator key
        if secrets.compare_digest(received_token, hub_secret):
            return UserIdentity(id=OPERATOR_ID, role="operator")

        # Check for Tamer session token
        tamer_identity = _tamer_identity_from_token(received_token)
        if tamer_identity is not None:
            return tamer_identity

        # Check for short-lived WS ticket (single-use, 60 s TTL)
        ticket_identity = _ws_ticket_identity(received_token)
        if ticket_identity is not None:
            return ticket_identity

    return None


def _ws_ticket_identity(token: str) -> UserIdentity | None:
    if not token.startswith(WS_TICKET_PREFIX):
        return None
    redeemed = database.redeem_ws_ticket(token)
    if redeemed is None:
        return None
    custodian_id = redeemed["custodian_id"]
    if redeemed["role"] == "operator":
        return UserIdentity(id=OPERATOR_ID, role="operator")
    if redeemed["role"] == "tamer":
        return UserIdentity(
            id=custodian_id,
            owner_id=custodian_id,
            custodian_id=custodian_id,
            role="tamer",
        )
    return UserIdentity(
        id=redeemed["soul_id"] or custodian_id,
        owner_id=custodian_id,
        custodian_id=custodian_id,
        role="user",
    )


async def require_scoped(
    identity: UserIdentity = Depends(get_api_key),
) -> UserIdentity:
    """Dependency gating custody operations: operators pass, everyone else
    must carry a custodian scope."""
    if identity.is_operator or identity.custodian_id:
        return identity
    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Custody scope required")


def assert_custody(identity: UserIdentity, custodian_id: str | None) -> None:
    """Deny cross-custody access. Operators bypass."""
    if identity.is_operator:
        return
    if not custodian_id or identity.custodian_id != custodian_id:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Cross-custody access denied"
        )
