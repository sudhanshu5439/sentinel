"""order-api: the entry point of the trading demo.

POST /orders  -> validate -> ask risk-engine -> store -> publish event -> return the order
GET  /orders/{id}, GET /orders?limit=N
/healthz (process alive), /readyz (dependencies reachable), /metrics (Prometheus)
"""

import asyncio
import logging
import random
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .clients import (
    EventPublisher,
    HttpRiskClient,
    NullEventPublisher,
    RedisEventPublisher,
    RiskClient,
)
from .config import Settings
from .models import Order, OrderRequest, OrderStatus
from .repository import InMemoryOrderRepository, OrderRepository, PostgresOrderRepository
from .telemetry import setup_logging, setup_metrics, setup_tracing

log = logging.getLogger("order_api")


def create_app(
    settings: Settings | None = None,
    repo: OrderRepository | None = None,
    risk: RiskClient | None = None,
    events: EventPublisher | None = None,
) -> FastAPI:
    """App factory: real dependencies by default, fakes injected by tests."""
    settings = settings or Settings.from_env()
    setup_logging(settings.service_name, settings.service_version)

    repo = repo or (
        PostgresOrderRepository(settings.database_url)
        if settings.database_url
        else InMemoryOrderRepository()
    )
    risk = risk or HttpRiskClient(settings.risk_engine_url, settings.risk_timeout_seconds)
    events = events or (
        RedisEventPublisher(settings.redis_url) if settings.redis_url else NullEventPublisher()
    )

    app = FastAPI(title="order-api", version=settings.service_version)

    # Order matters: Starlette runs the LAST-added middleware first (outermost). The fault
    # middleware is added first, so the metrics middleware added after it wraps it and counts
    # injected errors and delays, which is exactly what the canary analysis must see.
    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        # Simulates a bad release for the chaos scenarios. Off unless FAULT_* env vars are set.
        if request.url.path.startswith("/orders") and request.method == "POST":
            if settings.fault_latency_ms:
                await asyncio.sleep(settings.fault_latency_ms / 1000)
            if settings.fault_error_rate and random.random() < settings.fault_error_rate:  # noqa: S311
                log.error("injected fault: returning 500")
                return JSONResponse({"detail": "internal error"}, status_code=500)
        return await call_next(request)

    setup_metrics(app)
    setup_tracing(app, settings.service_name, settings.service_version)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        checks = {"database": repo.ping(), "events": events.ping()}
        ok = all(checks.values())
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

    @app.post("/orders", status_code=201)
    def place_order(req: OrderRequest) -> Order:
        try:
            decision = risk.check(req)
        except httpx.HTTPError as exc:
            log.error("risk-engine unavailable: %s", exc)
            raise HTTPException(status_code=503, detail="risk check unavailable") from exc

        order = Order(
            id=uuid4(),
            **req.model_dump(),
            status=OrderStatus.APPROVED if decision.approved else OrderStatus.REJECTED,
            reason=decision.reason,
            created_at=datetime.now(UTC),
        )
        repo.save(order)
        try:
            events.publish(order)
        except Exception:  # an event failure must not lose an order that is already stored
            log.exception("failed to publish order event %s", order.id)
        log.info(
            "order %s %s %s x%d -> %s",
            order.id,
            order.side,
            order.symbol,
            order.quantity,
            order.status,
        )
        return order

    @app.get("/orders/{order_id}")
    def get_order(order_id: UUID) -> Order:
        order = repo.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    @app.get("/orders")
    def list_orders(limit: int = Query(default=20, ge=1, le=200)) -> list[Order]:
        return repo.list_recent(limit)

    return app
