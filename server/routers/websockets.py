"""
WebSocket handlers for real-time presence and hub-wide communication.
"""

import hashlib
import hmac
import json
import logging
import secrets as pysecrets

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


def _verify_hmac(hmac_key: str, payload: str, signature: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    expected = hmac.new(hmac_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.websocket("/ws/{owner_id}")
async def websocket_presence(
    websocket: WebSocket,
    owner_id: str,
    token: str | None = Query(default=None),
    x_hub_secret: str | None = Header(default=None, alias="X-Hub-Secret"),
):
    """WebSocket endpoint for real-time presence tracking with HMAC binding."""
    # Authenticate
    identity = await verify_ws_token(websocket, token, x_hub_secret)
    if not identity:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid Credentials"
        )
        return

    # Verify authorization
    is_authorized = (
        identity.is_operator
        or identity.owner_id == owner_id
        or identity.id == owner_id
        or (identity.custodian_id is not None and identity.custodian_id == owner_id)
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

    # Fetch custodied souls for session creation and validation
    owned_soul_ids: set = set()
    if identity.is_user:
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT soul_id FROM souls WHERE COALESCE(custodian_id, owner_id) = ?",
                (owner_id,),
            )
            owned_soul_ids = {row["soul_id"] for row in cursor.fetchall()}

    # Create HMAC session for this connection
    hmac_key = pysecrets.token_urlsafe(32)
    session_soul_id = next(iter(owned_soul_ids)) if owned_soul_ids else None
    session_id = database.create_ws_session(owner_id, session_soul_id, hmac_key)

    await manager.connect(owner_id, websocket)

    try:
        # Send session info + current online owners to the newly connected client
        online = manager.get_online_owners()
        await websocket.send_json(
            {
                "type": "connected",
                "session_id": session_id,
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

                # Rate limit check (skip for operators)
                if identity.is_user and not database.check_rate_limit(
                    owner_id, cost=1.0
                ):
                    logger.warning(f"Rate limit exceeded for {owner_id}")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "RATE_LIMITED",
                            "message": "Too many requests",
                        }
                    )
                    continue

                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "soul_update":
                    # Verify HMAC signature
                    received_sig = message.get("signature")
                    session_id_recv = message.get("session_id")
                    nonce = message.get("nonce")
                    if not received_sig or not session_id_recv or not nonce:
                        logger.warning(
                            f"Missing HMAC signature/session_id/nonce from {owner_id}"
                        )
                        await websocket.close(
                            code=status.WS_1008_POLICY_VIOLATION,
                            reason="Missing signature/session/nonce",
                        )
                        return

                    session = database.get_ws_session(session_id_recv)
                    if not session or session["owner_id"] != owner_id:
                        logger.warning(
                            f"Invalid session from {owner_id}: {session_id_recv}"
                        )
                        await websocket.close(
                            code=status.WS_1008_POLICY_VIOLATION,
                            reason="Invalid session",
                        )
                        return

                    # Verify nonce (replay protection)
                    if not database.validate_and_store_nonce(session_id_recv, nonce):
                        logger.warning(
                            f"Replay attack detected from {owner_id}: nonce={nonce}"
                        )
                        await websocket.close(
                            code=status.WS_1008_POLICY_VIOLATION,
                            reason="Replay detected",
                        )
                        return

                    # Verify HMAC over the souls payload
                    souls_payload = json.dumps(
                        message.get("souls", []), separators=(",", ":")
                    )
                    if not _verify_hmac(
                        session["hmac_key"], souls_payload, received_sig
                    ):
                        logger.warning(f"HMAC verification failed from {owner_id}")
                        await websocket.close(
                            code=status.WS_1008_POLICY_VIOLATION,
                            reason="Invalid signature",
                        )
                        return

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

                    # Server-side movement validation
                    validated_souls = []
                    for soul in souls:
                        sid = soul.get("soul_id")
                        new_x = soul.get("x")
                        new_y = soul.get("y")
                        if sid and new_x is not None and new_y is not None:
                            # Fetch previous position and stats from DB
                            with database.get_db() as conn:
                                cursor = conn.cursor()
                                cursor.execute(
                                    "SELECT position, stat_spe_base FROM souls WHERE soul_id = ?",
                                    (sid,),
                                )
                                row = cursor.fetchone()
                                if row:
                                    import json as _json

                                    prev_pos = (
                                        _json.loads(row["position"])
                                        if row["position"]
                                        else [0, 0]
                                    )
                                    spe_stat = row["stat_spe_base"] or 0
                                    prev_x, prev_y = (
                                        float(prev_pos[0]),
                                        float(prev_pos[1]),
                                    )
                                    # Use a reasonable dt estimate (client sends ~30Hz)
                                    dt = 1.0 / 30.0
                                    try:
                                        clamped_x, clamped_y = (
                                            database.validate_soul_movement(
                                                sid,
                                                prev_x,
                                                prev_y,
                                                float(new_x),
                                                float(new_y),
                                                dt,
                                                spe_stat,
                                            )
                                        )
                                        soul["x"] = clamped_x
                                        soul["y"] = clamped_y
                                    except ValueError as e:
                                        logger.warning(
                                            f"Movement validation failed for {sid}: {e}"
                                        )
                                        continue
                        validated_souls.append(soul)

                    await manager.broadcast(
                        {
                            "type": "soul_updated",
                            "owner_id": owner_id,
                            "souls": validated_souls,
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
        database.delete_ws_session(session_id)
        manager.disconnect(owner_id)
        await manager.broadcast(
            {"type": "owner_offline", "owner_id": owner_id},
        )
