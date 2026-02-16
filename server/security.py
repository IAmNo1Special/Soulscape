import os
import secrets
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

        # Check for individual Soul secret
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id, owner_id FROM souls WHERE secret = ?",
                (api_key,),
            )
            row = cursor.fetchone()
            if row:
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

        # Check for individual Soul secret
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id, owner_id FROM souls WHERE secret = ?",
                (received_token,),
            )
            row = cursor.fetchone()
            if row:
                return UserIdentity(
                    id=row["soul_id"], owner_id=row["owner_id"], role="user"
                )

    return None
