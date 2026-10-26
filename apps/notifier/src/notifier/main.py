"""notifier: reads order events from the Redis Stream "orders" and sends a notification.

Uses a Redis consumer group, so several replicas share the work and every event is
acknowledged (XACK) only after it was handled: if a pod dies mid-way, the event is redelivered.
The "notification" is a structured log line for now; a real channel (email, Slack) plugs into
`send()`.
"""

import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

STREAM, GROUP = "orders", "notifier"
CONSUMER = os.getenv("HOSTNAME", "notifier-local")  # pod name = unique consumer name

log = logging.getLogger("notifier")
PROCESSED = Counter("notifier_events_processed_total", "Order events handled", ["status"])


def format_notification(order: dict) -> str:
    verb = "accepted" if order.get("status") == "APPROVED" else "rejected"
    return (
        f"Order {order.get('id')} for {order.get('account_id')}: {order.get('side')} "
        f"{order.get('quantity')} {order.get('symbol')} @ {order.get('price')} was {verb}"
        f" ({order.get('reason')})"
    )


class JsonFormatter(logging.Formatter):
    """One JSON object per log line (same shape as order-api), easy to query in Loki."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "service": "notifier",
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def send(message: str) -> None:
    log.info("notification sent", extra={"fields": {"event": "notification", "message": message}})


def ensure_group(r: redis.Redis) -> None:
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):  # group already exists: fine
            raise


def process_batch(r: redis.Redis, block_ms: int = 5000) -> int:
    """Read up to 10 new events, handle each, ack it. Returns how many were handled."""
    resp = r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=10, block=block_ms)
    handled = 0
    for _stream, entries in resp or []:
        for entry_id, fields in entries:
            try:
                raw = fields.get(b"order") or fields.get("order")
                send(format_notification(json.loads(raw)))
                PROCESSED.labels("ok").inc()
            except Exception:
                log.exception("failed to handle event %s", entry_id)
                PROCESSED.labels("error").inc()
            r.xack(
                STREAM, GROUP, entry_id
            )  # poison messages are logged and acked, not retried forever
            handled += 1
    return handled


class Worker:
    def __init__(self, r: redis.Redis) -> None:
        self.r, self._stop = r, threading.Event()
        self.thread = threading.Thread(target=self._run, name="consumer", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ensure_group(self.r)
                process_batch(self.r)
            except redis.RedisError as exc:
                log.warning("redis error, retrying in 2s: %s", exc)
                self._stop.wait(2)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()


def create_app(r: redis.Redis | None = None, start_worker: bool = True) -> FastAPI:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    r = r or redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=10, socket_connect_timeout=2
    )
    worker = Worker(r)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="notifier", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            redis_ok = bool(r.ping())
        except redis.RedisError:
            redis_ok = False
        worker_ok = worker.thread.is_alive() or not start_worker
        ok = redis_ok and worker_ok
        return JSONResponse(
            {"ready": ok, "checks": {"redis": redis_ok, "worker": worker_ok}},
            status_code=200 if ok else 503,
        )

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
