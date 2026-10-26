"""Request/response models. Pydantic validates input at the edge."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class OrderRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z0-9.\-]+$")
    side: Side
    quantity: int = Field(gt=0, le=1_000_000)
    price: float = Field(gt=0, le=1_000_000)


class RiskDecision(BaseModel):
    approved: bool
    reason: str


class Order(BaseModel):
    id: UUID
    account_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    status: OrderStatus
    reason: str
    created_at: datetime
