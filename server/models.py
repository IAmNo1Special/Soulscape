"""
Pydantic models for request and response validation in the Soulscape Hub API.
"""

import time
from typing import Any, Dict, List, Optional

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


class SocialEdit(BaseModel):
    author_id: str
    content: str


class MarketFundUpdate(BaseModel):
    amount: float


class BuyRequest(BaseModel):
    buyer_id: str


class MessageDelete(BaseModel):
    author_id: str


class SoulUpdate(BaseModel):
    owner_id: str
    souls: List[Dict[str, Any]]


class SoulResponse(BaseModel):
    soul_id: str
    owner_id: str
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
    position: Optional[List[float]] = [0.0, 0.0]
    hometown: Optional[Any] = None
    birth_date: Optional[str] = None
    activity: Optional[str] = None
    orb_color: Optional[List[float]] = [1.0, 1.0, 1.0]
    aura_color: Optional[List[float]] = [1.0, 1.0, 1.0]
    aura_visible: Optional[bool] = True
    nature: Optional[str] = "Hardy"
    inventory: Dict[str, int] = {}

    model_config = ConfigDict(from_attributes=True)
