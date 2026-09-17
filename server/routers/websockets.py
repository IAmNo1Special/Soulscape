"""
WebSocket handlers for real-time presence and hub-wide communication.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets as pysecrets

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from shared import protocol

from .. import database
from .. import dormancy
from .. import intents
from .. import market
from .. import plots
from .. import viewport
from .. import biology
from ..managers import manager
from ..models import WsTicketResponse
from ..security import (
    UserIdentity,
    require_scoped,
    verify_ws_token,
    WS_TICKET_TTL_SECONDS,
)

logger = logging.getLogger("soulscape_hub")

router = APIRouter()


@router.post("/ws/ticket", response_model=WsTicketResponse)
def mint_ws_ticket(identity: UserIdentity = Depends(require_scoped)):
    if identity.is_operator:
        ticket = database.create_ws_ticket(
            "", "operator", ttl_seconds=WS_TICKET_TTL_SECONDS
        )
    elif identity.is_tamer:
        ticket = database.create_ws_ticket(
            identity.custodian_id or "",
            "tamer",
            ttl_seconds=WS_TICKET_TTL_SECONDS,
        )
    else:
        ticket = database.create_ws_ticket(
            identity.custodian_id or "",
            "user",
            soul_id=identity.id,
            ttl_seconds=WS_TICKET_TTL_SECONDS,
        )
    return WsTicketResponse(ticket=ticket, expires_in=WS_TICKET_TTL_SECONDS)


def _verify_hmac(hmac_key: str, payload: str, signature: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    expected = hmac.new(hmac_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _intent_authn(message: dict, owner_id: str) -> tuple[dict | None, str | None]:
    received_sig = message.get("signature")
    session_id = message.get("session_id")
    nonce = message.get("nonce")
    if not received_sig or not session_id or not nonce:
        return None, "Missing signature/session/nonce"
    session = database.get_ws_session(session_id)
    if not session or session["owner_id"] != owner_id:
        return None, "Invalid session"
    if not _verify_hmac(
        session["hmac_key"], intents.canonical_intent(message), received_sig
    ):
        return None, "Invalid signature"
    return session, None


def _intent_custodian(identity: UserIdentity) -> str | None:
    if identity.is_operator:
        return None
    return identity.custodian_id or identity.owner_id


async def _handle_tamer_presence(
    websocket: WebSocket,
    message: dict,
    identity: UserIdentity,
    session_id: str,
    nonce: str,
    payload: dict,
) -> None:
    """Tamer-scoped presence intent (#28).

    The tamer is the authenticated identity -- there is no client field
    naming a tamer, so cross-tamer spoofing is impossible by construction.
    Operators are rejected: they carry no tamer scope.
    """
    from .. import presence as presence_module

    tamer_id = _intent_custodian(identity)
    if tamer_id is None:
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code="CUSTODY_DENIED",
                message="Intent rejected: CUSTODY_DENIED (presence is tamer-scoped)",
                nonce=nonce,
            )
        )
        return
    if payload.get("app_category") is not None and not (
        presence_module.get_app_opt_in(tamer_id)
    ):
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code="BAD_PAYLOAD",
                message="Intent rejected: BAD_PAYLOAD:app_category_no_opt_in",
                nonce=nonce,
            )
        )
        return
    try:
        record = presence_module.enqueue_presence_intent(
            session_id, nonce, tamer_id, payload
        )
    except ValueError as exc:
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code="BAD_PAYLOAD",
                message=f"Intent rejected: BAD_PAYLOAD ({exc})",
                nonce=nonce,
            )
        )
        return
    await websocket.send_json(_intent_ack(record))


def _intent_ack(record: dict) -> dict:
    ack_status = {"pending": "accepted"}.get(record["status"], record["status"])
    ack = protocol.envelope(
        protocol.MessageType.INTENT_ACK,
        nonce=record["nonce"],
        intent_id=record["intent_id"],
        status=ack_status,
    )
    if record["status"] not in ("pending",) and record.get("result") is not None:
        ack["result"] = record["result"]
    return ack


async def _handle_intent(
    websocket: WebSocket,
    message: dict,
    identity: UserIdentity,
    owner_id: str,
) -> None:
    session, reason = _intent_authn(message, owner_id)
    if session is None:
        logger.warning(f"Intent auth failed from {owner_id}: {reason}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason)
        return
    session_id = session["session_id"]
    nonce = message["nonce"]
    if not database.validate_and_store_nonce(session_id, nonce):
        existing = intents.get_intent_by_nonce(session_id, nonce)
        if existing is not None:
            await websocket.send_json(_intent_ack(existing))
            return
    kind = message.get("kind")
    payload, error = intents.validate_payload(kind, message)
    if error is not None:
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code=error,
                message=f"Intent rejected: {error}",
                nonce=nonce,
            )
        )
        return
    if kind == "tamer_presence":
        await _handle_tamer_presence(
            websocket, message, identity, session_id, nonce, payload
        )
        return
    soul_id = message.get("soul_id")
    custodian = _intent_custodian(identity)
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT custodian_id, owner_id, COALESCE(essence, 0.0) AS essence "
            "FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    if row is None:
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code="SOUL_NOT_FOUND",
                message="Intent rejected: SOUL_NOT_FOUND",
                nonce=nonce,
            )
        )
        return
    if custodian is not None and (row["custodian_id"] or row["owner_id"]) != custodian:
        logger.warning(f"Spoofing attempt: {owner_id} intent for soul_id={soul_id}")
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code="CUSTODY_DENIED",
                message="Intent rejected: CUSTODY_DENIED",
                nonce=nonce,
            )
        )
        return
    if kind in plots.PLOT_KINDS:
        try:
            record, _created = plots.enqueue_plot_intent(
                session_id, nonce, custodian, soul_id, kind, payload
            )
        except plots.PlotRefusal as refusal:
            await websocket.send_json(
                protocol.envelope(
                    protocol.MessageType.ERROR,
                    code=refusal.ws_code,
                    message=f"Intent rejected: {refusal.ws_code}",
                    nonce=nonce,
                )
            )
            return
        await websocket.send_json(_intent_ack(record))
        return
    if kind in market.MARKET_KINDS:
        try:
            record, _created = market.enqueue_market_intent(
                session_id, nonce, custodian, soul_id, kind, payload
            )
        except market.MarketRefusal as refusal:
            await websocket.send_json(
                protocol.envelope(
                    protocol.MessageType.ERROR,
                    code=refusal.ws_code,
                    message=f"Intent rejected: {refusal.ws_code}",
                    nonce=nonce,
                )
            )
            return
        await websocket.send_json(_intent_ack(record))
        return
    if kind in biology.BIOLOGY_KINDS:
        try:
            record, _created = biology.enqueue_feed_soul(
                session_id, nonce, custodian, soul_id, kind, payload
            )
        except biology.BiologyRefusal as refusal:
            await websocket.send_json(
                protocol.envelope(
                    protocol.MessageType.ERROR,
                    code=refusal.ws_code,
                    message=f"Intent rejected: {refusal.ws_code}",
                    nonce=nonce,
                )
            )
            return
        await websocket.send_json(_intent_ack(record))
        return
    # Generic kinds (move_to): dormancy is rejected early here for a fast,
    # clear error; _adjudicate_move_to re-checks at the tick pump (defense
    # in depth -- kind-specific enqueues above do their own checks).
    # Dormancy (issue #22): statues don't act.
    if dormancy.is_dormant(row["essence"]):
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.ERROR,
                code="SOUL_DORMANT",
                message="Intent rejected: SOUL_DORMANT",
                nonce=nonce,
            )
        )
        return
    record = intents.enqueue_intent(
        session_id, nonce, custodian, soul_id, kind, payload
    )
    await websocket.send_json(_intent_ack(record))


def _current_tick_id(websocket: WebSocket) -> int:
    tick = getattr(websocket.app.state, "world_tick", None)
    return tick.tick_id if tick is not None else 0


async def _viewport_pump(
    websocket: WebSocket, session: viewport.ViewportSession
) -> None:
    try:
        while True:
            await asyncio.sleep(session.flush_interval)
            positions = viewport.read_positions()
            states = viewport.read_soul_states()
            dormant = viewport.read_dormancy()
            biology = viewport.read_biology()
            tick_id = _current_tick_id(websocket)
            for op, domain in viewport.diff_positions(positions, session.committed):
                session.enqueue(op, domain)
            for op, domain in viewport.diff_states(states, session.committed_states):
                session.enqueue(op, domain)
            for op, domain in viewport.diff_dormancy(
                dormant, session.committed_dormancy
            ):
                session.enqueue(op, domain)
            for op, domain in viewport.diff_biology(
                biology, session.committed_biology
            ):
                session.enqueue(op, domain)
            result = await viewport.flush(
                session, positions, tick_id, websocket.send_json,
                states=states, dormant=dormant, biology=biology,
            )
            if result == "closed":
                await websocket.close(
                    code=status.WS_1013_TRY_AGAIN_LATER,
                    reason="resumable: slow_consumer",
                )
                return
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Viewport pump error: {e}")


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

    vp_session = viewport.viewport.create(owner_id)
    tick_id = _current_tick_id(websocket)
    pump_task = asyncio.create_task(_viewport_pump(websocket, vp_session))

    try:
        # Send session info + current online owners to the newly connected client
        online = manager.get_online_owners()
        await websocket.send_json(
            protocol.envelope(
                protocol.MessageType.CONNECTED,
                session_id=session_id,
                conn_id=vp_session.conn_id,
                hmac_key=hmac_key,
                online_owners=[oid for oid in online if oid != owner_id],
            )
        )

        # Initial viewport snapshot (protocol v2 downstream)
        await websocket.send_json(
            viewport.build_snapshot(vp_session, protocol.SnapReason.JOIN, tick_id)
        )

        # Broadcast to others that this owner came online
        await manager.broadcast(
            protocol.envelope(protocol.MessageType.OWNER_ONLINE, owner_id=owner_id),
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
                        protocol.envelope(
                            protocol.MessageType.ERROR,
                            code="RATE_LIMITED",
                            message="Too many requests",
                        )
                    )
                    continue

                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "resume":
                    outcome = await viewport.viewport.resume(
                        vp_session,
                        message.get("conn_id"),
                        message.get("last_seq"),
                        _current_tick_id(websocket),
                        websocket.send_json,
                    )
                    if outcome == "snapshot":
                        await websocket.send_json(
                            viewport.build_snapshot(
                                vp_session,
                                protocol.SnapReason.RESYNC,
                                _current_tick_id(websocket),
                            )
                        )
                    continue

                if msg_type == "soul_update":
                    logger.warning(f"Removed channel used by {owner_id}: soul_update")
                    await websocket.send_json(
                        protocol.envelope(
                            protocol.MessageType.ERROR,
                            code="CHANNEL_REMOVED",
                            message=(
                                "soul_update removed: express intent with "
                                "'intent' frames"
                            ),
                        )
                    )
                    continue

                if msg_type == "intent":
                    await _handle_intent(websocket, message, identity, owner_id)
                    continue
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Error processing message from {owner_id}: {e}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {owner_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {owner_id}: {e}")
    finally:
        pump_task.cancel()
        vp_session.live = False
        database.delete_ws_session(session_id)
        manager.disconnect(owner_id)
        await manager.broadcast(
            protocol.envelope(protocol.MessageType.OWNER_OFFLINE, owner_id=owner_id),
        )
