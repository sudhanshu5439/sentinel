"""Outbound dependencies: the risk-engine (HTTP) and the event stream (Redis)."""

import json
import logging
from typing import Protocol

import httpx
import redis

from .models import Order, OrderRequest, RiskDecision

log = logging.getLogger(__name__)
ORDER_STREAM = "orders"


class RiskClient(Protocol):
    def check(self, req: OrderRequest) -> RiskDecision: ...


class HttpRiskClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def check(self, req: OrderRequest) -> RiskDecision:
        resp = self._client.post("/v1/check", json=req.model_dump(mode="json"))
        resp.raise_for_status()
        return RiskDecision.model_validate(resp.json())


class EventPublisher(Protocol):
    def publish(self, order: Order) -> None: ...
    def ping(self) -> bool: ...


class RedisEventPublisher:
    """Appends each order to a Redis Stream; the notifier reads it with a consumer group."""

    def __init__(self, url: str) -> None:
        self._redis = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)

    def publish(self, order: Order) -> None:
        self._redis.xadd(
            ORDER_STREAM,
            {"order": json.dumps(order.model_dump(mode="json"))},
            maxlen=10_000,
            approximate=True,
        )

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except redis.RedisError:
            return False


class NullEventPublisher:
    def publish(self, order: Order) -> None:
        log.debug("event publishing disabled; order %s not published", order.id)

    def ping(self) -> bool:
        return True
