import json

import fakeredis
from fastapi.testclient import TestClient

from notifier.main import (
    GROUP,
    STREAM,
    create_app,
    ensure_group,
    format_notification,
    process_batch,
)

ORDER = {
    "id": "o1",
    "account_id": "acc-1",
    "side": "BUY",
    "quantity": 10,
    "symbol": "INFY",
    "price": 1500.5,
    "status": "APPROVED",
    "reason": "within limits",
}


def test_format_notification():
    msg = format_notification(ORDER)
    assert "accepted" in msg and "INFY" in msg
    assert "rejected" in format_notification({**ORDER, "status": "REJECTED"})


def test_events_are_handled_and_acked():
    r = fakeredis.FakeRedis()
    ensure_group(r)
    ensure_group(r)  # second call must not fail (BUSYGROUP)
    r.xadd(STREAM, {"order": json.dumps(ORDER)})
    r.xadd(STREAM, {"order": "not-json"})  # poison message: logged and acked, not retried forever
    assert process_batch(r, block_ms=10) == 2
    assert r.xpending(STREAM, GROUP)["pending"] == 0


def test_health_and_metrics():
    client = TestClient(create_app(fakeredis.FakeRedis(), start_worker=False))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").json()["checks"]["redis"] is True
    assert "notifier_events_processed_total" in client.get("/metrics").text


def test_log_lines_are_json(capsys):
    import logging

    from notifier.main import JsonFormatter, send

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.getLogger("notifier").addHandler(handler)
    logging.getLogger("notifier").setLevel(logging.INFO)
    send("hello")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["event"] == "notification" and entry["message"] == "hello"
