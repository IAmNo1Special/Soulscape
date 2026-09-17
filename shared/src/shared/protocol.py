from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class MessageType(str, Enum):
    CONNECTED = "connected"
    OWNER_ONLINE = "owner_online"
    OWNER_OFFLINE = "owner_offline"
    SOUL_UPDATED = "soul_updated"
    SOUL_UPDATE = "soul_update"
    SNAPSHOT = "snapshot"
    DELTA = "delta"
    INTENT = "intent"
    INTENT_ACK = "intent_ack"
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


class SnapReason(str, Enum):
    FULL_SYNC = "full_sync"
    JOIN = "join"
    RESYNC = "resync"
    TICK = "tick"


class EntityOpKind(str, Enum):
    UPSERT = "upsert"
    REMOVE = "remove"


class SoulWireState(BaseModel):
    model_config = ConfigDict(extra="allow")

    soul_id: str
    x: float = 0.0
    y: float = 0.0


class EntityOp(BaseModel):
    op: EntityOpKind
    soul_id: str
    state: SoulWireState | None = None


class Snapshot(BaseModel):
    reason: SnapReason
    tick_id: int | None = None
    souls: list[SoulWireState] = Field(default_factory=list)


class Delta(BaseModel):
    tick_id: int | None = None
    ops: list[EntityOp] = Field(default_factory=list)


class Intent(BaseModel):
    model_config = ConfigDict(extra="allow")

    intent: str
    soul_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    signature: str | None = None
    nonce: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    v: int = PROTOCOL_VERSION
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


def envelope(msg_type: str | MessageType, **fields: Any) -> dict[str, Any]:
    msg_type = msg_type.value if isinstance(msg_type, MessageType) else msg_type
    return {"v": PROTOCOL_VERSION, "type": msg_type, **fields}


def parse_envelope(data: dict[str, Any]) -> Envelope:
    return Envelope.model_validate(data)


def is_legacy(data: dict[str, Any]) -> bool:
    return "v" not in data
