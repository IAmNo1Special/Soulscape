"""
WebSocket handlers for real-time presence and hub-wide communication.
"""

import json
import logging

from fastapi import (
    APIRouter,
    Header,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from .. import database
from ..managers import manager
from ..security import verify_ws_token

logger = logging.getLogger("soulscape_hub")

router = APIRouter()


@router.websocket("/ws/{owner_id}")
async def websocket_presence(
    websocket: WebSocket,
    owner_id: str,
    token: str | None = Query(default=None),
    x_hub_secret: str | None = Header(default=None, alias="X-Hub-Secret"),
):
    """WebSocket endpoint for real-time presence tracking."""
    # Authenticate
    identity = await verify_ws_token(websocket, token, x_hub_secret)
    if not identity:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid Credentials"
        )
        return

    # Verify authorization
    # Operator can connect as anyone, but regular users must match their owner_id
    is_authorized = (
        identity.is_operator or identity.owner_id == owner_id or identity.id == owner_id
    )

    if not is_authorized:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Unauthorized: Cannot connect as another owner",
        )
        return

    if not owner_id:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Owner ID REQUIRED"
        )
        return

    await manager.connect(owner_id, websocket)
    owned_soul_ids: set = set()
    if identity.is_user:
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id FROM souls WHERE owner_id = ?",
                (owner_id,),
            )
            owned_soul_ids = {row["soul_id"] for row in cursor.fetchall()}
    try:
        # Send current online owners to the newly connected client
        online = manager.get_online_owners()
        await websocket.send_json(
            {
                "type": "connected",
                "online_owners": [oid for oid in online if oid != owner_id],
            }
        )

        # Broadcast to others that this owner came online
        await manager.broadcast(
            {"type": "owner_online", "owner_id": owner_id},
            exclude=owner_id,
        )

        while True:
            data = await websocket.receive_text()
            try:
                if data == "ping":
                    await websocket.send_text("pong")
                    continue

                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "soul_update":
                    souls = message.get("souls", [])
                    if identity.is_user:
                        for soul in souls:
                            sid = soul.get("soul_id")
                            if sid and sid not in owned_soul_ids:
                                logger.warning(
                                    f"Spoofing attempt: {owner_id} sent soul_id={sid} not owned"
                                )
                                await websocket.close(
                                    code=status.WS_1008_POLICY_VIOLATION,
                                    reason="Invalid soul_id",
                                )
                                return
                    await manager.broadcast(
                        {
                            "type": "soul_updated",
                            "owner_id": owner_id,
                            "souls": souls,
                        },
                        exclude=owner_id,
                    )
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Error processing message from {owner_id}: {e}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {owner_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {owner_id}: {e}")
    finally:
        manager.disconnect(owner_id)
        await manager.broadcast(
            {"type": "owner_offline", "owner_id": owner_id},
        )
