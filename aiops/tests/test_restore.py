"""A restart must not forget incidents: rebuilt from the audit log (8b run 3 showed the gap)."""

from datetime import timedelta

from conftest import make_alert
from fastapi.testclient import TestClient
from test_api import SETTINGS, payload
from test_diagnosis import FakeLLM, answer

from assistant.audit import MemoryAudit
from assistant.grouping import IncidentGrouper
from assistant.main import create_app
from assistant.models import IncidentStatus

HONEST = answer(
    category="unknown",
    confidence="low",
    probable_cause={"statement": "no change before the incident", "evidence": ["E1"]},
    supporting=[],
)


def app(clock, audit, llm=None):
    grouper = IncidentGrouper(window=timedelta(minutes=15), clock=clock)
    return grouper, create_app(SETTINGS, grouper, collectors=[], llm=llm, audit=audit)


def test_after_a_restart_resolved_alerts_still_close_their_incident(clock):
    audit = MemoryAudit()
    alert = make_alert(namespace="sentinel-staging")
    _, first = app(clock, audit, FakeLLM(HONEST))
    with TestClient(first) as c:
        (incident_id,) = c.post("/webhook/alertmanager", json=payload(alert)).json()[
            "new_incidents"
        ]
    assert [r["event"] for r in audit.rows] == ["opened", "diagnosis", "decision"]

    # "Restart": a new process with an empty grouper, the same audit log, and an LLM that would
    # fail the test if it were called again.
    grouper, second = app(clock, audit, FakeLLM())
    with TestClient(second) as c:
        restored = grouper.get(incident_id)
        assert restored is not None and restored.status is IncidentStatus.OPEN
        assert restored.diagnosis is not None and restored.decision is not None

        # Alertmanager re-sends the still-firing alert: a duplicate, not a second investigation.
        again = c.post("/webhook/alertmanager", json=payload(alert)).json()
        assert again["new_incidents"] == []

        resolved = alert.model_copy(update={"status": "resolved"})
        c.post("/webhook/alertmanager", json=payload(resolved))
    assert grouper.get(incident_id).status is IncidentStatus.RESOLVED
    assert audit.rows[-1]["event"] == "resolved"

    # A third start knows the incident is over, and the next alert for the same service is
    # handled. Regression: restored timestamps stayed strings and this webhook crashed (500).
    clock.advance(30)
    grouper, third = app(clock, audit, FakeLLM(HONEST))
    with TestClient(third) as c:
        restored = grouper.get(incident_id)
        assert restored.status is IncidentStatus.RESOLVED
        assert isinstance(restored.last_activity_at, type(clock())) and restored.resolved_at
        r = c.post("/webhook/alertmanager", json=payload(make_alert(namespace="sentinel-staging")))
        assert r.status_code == 200 and len(r.json()["new_incidents"]) == 1


def test_an_incident_accepted_but_not_diagnosed_before_a_restart_is_investigated_again(clock):
    """Step 9 pilot: a deploy killed the assistant 60 s into a DB-outage investigation;
    Alertmanager had its 200 and would not re-send for 12 h, so the incident was lost."""
    from datetime import UTC, datetime

    from assistant.audit import opened_row

    audit = MemoryAudit()
    accepted = IncidentGrouper(window=timedelta(minutes=15), clock=lambda: datetime.now(UTC))
    incident = accepted.ingest(make_alert(namespace="sentinel-staging")).incident
    audit.rows.append(
        {
            "incident_id": incident.id,
            "namespace": incident.namespace,
            "service": incident.service,
            **opened_row(incident),
        }
    )  # ...and then the process died: no diagnosis row

    llm = FakeLLM(HONEST)
    grouper, restarted = app(clock, audit, llm)
    with TestClient(restarted):
        pass  # start-up re-investigates; shutdown waits for it to finish
    assert len(llm.prompts) == 1
    assert grouper.get(incident.id).diagnosis is not None
    assert [r["event"] for r in audit.rows] == ["opened", "diagnosis", "decision"]


def test_rows_that_do_not_parse_are_skipped_not_fatal(clock):
    audit = MemoryAudit()
    audit.rows.append(
        {
            "incident_id": "garbage",
            "namespace": "x",
            "service": "y",
            "event": "diagnosis",
            "payload": {"alerts": [{"nonsense": True}]},
        }
    )
    grouper, a = app(clock, audit)
    with TestClient(a) as c:
        assert c.get("/healthz").status_code == 200
    assert grouper.list() == []
