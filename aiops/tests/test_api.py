from datetime import timedelta

from conftest import make_alert
from fastapi.testclient import TestClient

from assistant.config import Settings
from assistant.grouping import IncidentGrouper
from assistant.main import create_app

SETTINGS = Settings(
    service_name="sentinel-assistant",
    service_version="test",
    group_window_minutes=15,
    max_incidents=100,
)


def client(clock) -> TestClient:
    grouper = IncidentGrouper(window=timedelta(minutes=15), clock=clock)
    return TestClient(create_app(SETTINGS, grouper))


def payload(*alerts) -> dict:
    """An Alertmanager webhook body (version 4), as it arrives on the wire."""
    return {
        "version": "4",
        "status": "firing",
        "receiver": "sentinel-assistant",
        "groupKey": '{}:{alertname="OrderApiAvailabilityBudgetBurnFast"}',
        "truncatedAlerts": 0,
        "groupLabels": {"alertname": "OrderApiAvailabilityBudgetBurnFast"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": [a.model_dump(mode="json", by_alias=True) for a in alerts],
    }


def test_health_endpoints(clock):
    c = client(clock)
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 200


def test_webhook_opens_one_incident_for_a_storm(clock):
    c = client(clock)
    body = payload(make_alert(alertname="A"), make_alert(alertname="B"), make_alert(alertname="A"))

    r = c.post("/webhook/alertmanager", json=body)
    assert r.status_code == 200
    assert r.json()["received"] == 3
    assert len(r.json()["new_incidents"]) == 1

    again = c.post("/webhook/alertmanager", json=body)  # Alertmanager re-sends the group
    assert again.json()["new_incidents"] == []

    incidents = c.get("/incidents").json()
    assert len(incidents) == 1
    assert incidents[0]["service"] == "order-api"
    assert incidents[0]["namespace"] == "staging"
    assert len(incidents[0]["alerts"]) == 2


def test_get_incident_by_id_and_404(clock):
    c = client(clock)
    incident_id = c.post("/webhook/alertmanager", json=payload(make_alert())).json()[
        "new_incidents"
    ][0]
    assert c.get(f"/incidents/{incident_id}").json()["id"] == incident_id
    assert c.get("/incidents/nope").status_code == 404


def test_malformed_payload_is_rejected(clock):
    c = client(clock)
    assert c.post("/webhook/alertmanager", json={"alerts": [{"status": "x"}]}).status_code == 422
