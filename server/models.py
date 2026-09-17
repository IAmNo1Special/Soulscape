"""
Pydantic models for request and response validation in the Soulscape Hub API.
"""

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MarketListing(BaseModel):
    listing_id: Optional[str] = None
    seller_id: str
    seller_name: str
    item: Dict[str, Any]
    price: float = Field(gt=0)
    timestamp: float = Field(default_factory=time.time)


class SocialPost(BaseModel):
    message_id: Optional[str] = None
    author_id: str
    author_name: str
    title: str = ""
    content: str
    timestamp: float = Field(default_factory=time.time)
    parent_id: Optional[str] = None
    replies: List[Dict[str, Any]] = []


class SocialReplyResponse(BaseModel):
    reply_id: str
    parent_id: str
    author_id: str
    author_name: str
    content: str
    timestamp: float


class SocialPostResponse(BaseModel):
    message_id: str
    author_id: str
    author_name: str
    title: str = ""
    content: str
    timestamp: float
    replies: List[SocialReplyResponse] = []


class SocialMessageNode(BaseModel):
    """One node of the unified threaded social tree (issue #18).

    ``content``/``timestamp`` are legacy aliases of ``body``/
    ``created_at`` kept for the desktop client, which reads those names.
    """

    message_id: str
    parent_id: Optional[str] = None
    author_type: str
    author_id: str
    author_name: str = ""
    title: Optional[str] = None
    body: str = ""
    content: str = ""
    created_at: float = 0.0
    timestamp: float = 0.0
    edited_at: Optional[float] = None
    deleted: bool = False
    replies: List["SocialMessageNode"] = []


class SocialEdit(BaseModel):
    author_id: str
    content: str


class MarketFundUpdate(BaseModel):
    amount: float


class BuyRequest(BaseModel):
    buyer_id: str


class MessageDelete(BaseModel):
    author_id: str


class FeedSoulRequest(BaseModel):
    feeder_soul_id: str
    recipient_soul_id: str


class QuipRequest(BaseModel):
    prompt: Optional[str] = None


class SoulUpdate(BaseModel):
    owner_id: Optional[str] = None
    custodian_id: Optional[str] = None
    souls: List[Dict[str, Any]]


class SoulResponse(BaseModel):
    soul_id: str
    owner_id: str
    custodian_id: Optional[str] = None
    name: Optional[str] = None
    first_name: Optional[str] = None
    family_name: Optional[str] = None
    species: Optional[str] = None
    gender: Optional[str] = None
    level: Optional[int] = 1
    xp: Optional[int] = 0
    hp: Optional[float] = 100.0
    max_hp: Optional[float] = 100.0
    satiety: Optional[float] = 100.0
    hydration: Optional[float] = 100.0
    essence: Optional[float] = 0.0
    state: Optional[str] = "normal"
    fed_flag: Optional[int] = 0
    position: Optional[List[float]] = [0.0, 0.0]
    velocity: Optional[List[float]] = [0.0, 0.0]
    hometown: Optional[Any] = None
    birth_date: Optional[str] = None
    activity: Optional[str] = None
    orb_color: Optional[List[float]] = [1.0, 1.0, 1.0]
    aura_color: Optional[List[float]] = [1.0, 1.0, 1.0]
    aura_visible: Optional[bool] = True
    nature: Optional[str] = "Hardy"
    stat_hp_base: Optional[int] = 0
    stat_atk_base: Optional[int] = 0
    stat_def_base: Optional[int] = 0
    stat_spa_base: Optional[int] = 0
    stat_spd_base: Optional[int] = 0
    stat_spe_base: Optional[int] = 0
    stat_vis_base: Optional[int] = 0
    stat_hp_iv: Optional[int] = 0
    stat_atk_iv: Optional[int] = 0
    stat_def_iv: Optional[int] = 0
    stat_spa_iv: Optional[int] = 0
    stat_spd_iv: Optional[int] = 0
    stat_spe_iv: Optional[int] = 0
    stat_vis_iv: Optional[int] = 0
    stat_hp_ev: Optional[int] = 0
    stat_atk_ev: Optional[int] = 0
    stat_def_ev: Optional[int] = 0
    stat_spa_ev: Optional[int] = 0
    stat_spd_ev: Optional[int] = 0
    stat_spe_ev: Optional[int] = 0
    stat_vis_ev: Optional[int] = 0
    inventory: Dict[str, int] = {}

    model_config = ConfigDict(from_attributes=True)


class TamerRegister(BaseModel):
    username: str
    password: str


class TamerLogin(BaseModel):
    username: str
    password: str


class TamerResponse(BaseModel):
    tamer_id: str
    username: str


class TamerSessionResponse(BaseModel):
    token: str
    tamer_id: str
    username: str
    expires_at: float


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int


class KeyUploadRequest(BaseModel):
    provider: str
    key: str
    label: str = ""


class KeyRotateRequest(BaseModel):
    new_key: str


class KeyMetadata(BaseModel):
    key_id: str
    tamer_id: str
    provider: str
    label: str
    last4: str
    created_at: float
    rotated_at: Optional[float] = None
    revoked_at: Optional[float] = None
    superseded_by: Optional[str] = None


class PricingUpdate(BaseModel):
    """Operator pricing-knob update (issue #27). Both fields optional;
    omitted fields keep their current values. Affects new settlements
    only."""

    essence_per_usd: Optional[float] = None
    model_rates: Optional[Dict[str, List[float]]] = None


class TamerPresenceReport(BaseModel):
    """Strict REST schema for the privacy-gated presence payload (#28).

    `extra="forbid"`: padded/unknown fields are rejected with 422. Enum
    values are closed; unknown values are rejected with 422.
    """

    model_config = ConfigDict(extra="forbid")

    presence: Literal["active", "idle", "locked", "away"]
    idle_bucket: Literal["0-5", "5-30", "30+"]
    event: Optional[Literal["tamer_return", "lock", "unlock"]] = None
    app_category: Optional[
        Literal["game", "browser", "media", "chat", "work", "other"]
    ] = None


class PresenceOptIn(BaseModel):
    """Tamer-scoped toggle for the optional app-category signal (#28)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class TamerPresenceState(BaseModel):
    tamer_id: str
    presence: Optional[str] = None
    idle_bucket: Optional[str] = None
    last_event: Optional[str] = None
    app_category: Optional[str] = None
    updated_at: Optional[float] = None
    stale: bool = False
