import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Security, WebSocket, status
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
    id: str  # Soul ID or Operator ID
    owner_id: Optional[str] = None
    role: str

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"

    @property
    def is_user(self) -> bool:
        return self.role == "user"


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

        # Check for individual Soul secret (hashed lookup)
        prefix = database.hash_secret(api_key)[:16]
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id, owner_id, token_expiry, is_revoked, secret_hash "
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
                        id=row["soul_id"], owner_id=row["owner_id"], role="user"
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

        # Check for individual Soul secret (hashed lookup)
        prefix = database.hash_secret(received_token)[:16]
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id, owner_id, token_expiry, is_revoked, secret_hash "
                "FROM souls WHERE secret_prefix = ?",
                (prefix,),
            )
            for row in cursor.fetchall():
                if database.verify_secret_hash(received_token, row["secret_hash"]):
                    if row["is_revoked"]:
                        return None
                    if row["token_expiry"] and row["token_expiry"] < time.time():
                        return None
                    return UserIdentity(
                        id=row["soul_id"], owner_id=row["owner_id"], role="user"
                    )

    return None
