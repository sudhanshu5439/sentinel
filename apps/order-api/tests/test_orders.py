import httpx
import pytest
from fastapi.testclient import TestClient

from order_api.config import Settings
from order_api.main import create_app
from order_api.models import OrderRequest, RiskDecision
from order_api.repository import InMemoryOrderRepository

VALID = {"account_id": "acc-1", "symbol": "INFY", "side": "BUY", "quantity": 10, "price": 1500.5}


class FakeRisk:
    def __init__(self, approved=True, fail=False):
        self.approved, self.fail, self.calls = approved, fail, 0

    def check(self, req: OrderRequest) -> RiskDecision:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("down")
        return RiskDecision(approved=self.approved, reason="ok" if self.approved else "limit")


class FakeEvents:
    def __init__(self, fail=False):
        self.published, self.fail = [], fail

    def publish(self, order):
        if self.fail:
            raise RuntimeError("redis down")
        self.published.append(order)

    def ping(self):
        return not self.fail


def settings(**overrides) -> Settings:
    base = dict(
        service_name="order-api",
        service_version="test",
        database_url=None,
        redis_url=None,
        risk_engine_url="http://x",
        risk_timeout_seconds=1.0,
        fault_error_rate=0.0,
        fault_latency_ms=0,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def make_client():
    def _make(risk=None, events=None, **cfg):
        app = create_app(
            settings(**cfg), InMemoryOrderRepository(), risk or FakeRisk(), events or FakeEvents()
        )
        return TestClient(app)

    return _make


def test_approved_order_is_stored_and_published(make_client):
    events = FakeEvents()
    client = make_client(events=events)
    resp = client.post("/orders", json=VALID)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert client.get(f"/orders/{body['id']}").json()["id"] == body["id"]
    assert len(events.published) == 1


def test_rejected_order_keeps_reason(make_client):
    resp = make_client(risk=FakeRisk(approved=False)).post("/orders", json=VALID)
    assert resp.status_code == 201
    assert resp.json()["status"] == "REJECTED"
    assert resp.json()["reason"] == "limit"


@pytest.mark.parametrize(
    "field,value", [("quantity", 0), ("price", -1), ("symbol", "bad sym"), ("side", "HOLD")]
)
def test_invalid_input_rejected_before_risk_call(make_client, field, value):
    risk = FakeRisk()
    resp = make_client(risk=risk).post("/orders", json={**VALID, field: value})
    assert resp.status_code == 422
    assert risk.calls == 0


def test_risk_engine_down_returns_503(make_client):
    assert make_client(risk=FakeRisk(fail=True)).post("/orders", json=VALID).status_code == 503


def test_event_failure_does_not_lose_order(make_client):
    client = make_client(events=FakeEvents(fail=True))
    resp = client.post("/orders", json=VALID)
    assert resp.status_code == 201
    assert client.get(f"/orders/{resp.json()['id']}").status_code == 200


def test_readyz_reports_dependency_failure(make_client):
    resp = make_client(events=FakeEvents(fail=True)).get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["events"] is False


def test_fault_injection_returns_500(make_client):
    assert make_client(fault_error_rate=1.0).post("/orders", json=VALID).status_code == 500


def test_unknown_order_404(make_client):
    resp = make_client().get("/orders/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_metrics_exposed(make_client):
    client = make_client()
    client.post("/orders", json=VALID)
    assert "http_requests_total" in client.get("/metrics").text
