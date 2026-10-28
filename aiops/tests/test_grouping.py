from datetime import timedelta

from conftest import make_alert

from assistant.grouping import IncidentGrouper, Outcome
from assistant.models import IncidentStatus


def grouper(clock, **kwargs) -> IncidentGrouper:
    return IncidentGrouper(window=timedelta(minutes=15), clock=clock, **kwargs)


def test_alert_storm_becomes_one_incident(clock):
    g = grouper(clock)
    names = ["AvailabilityFast", "AvailabilitySlow", "LatencyFast", "LatencySlow"]
    outcomes = []
    for i in range(10):  # 4 distinct alerts, re-sent repeatedly as Alertmanager does
        outcomes.append(g.ingest(make_alert(alertname=names[i % 4])).outcome)
        clock.advance(1)

    assert outcomes.count(Outcome.NEW_INCIDENT) == 1  # one storm, one diagnosis
    assert len(g.list()) == 1
    incident = g.list()[0]
    assert len(incident.alerts) == 4
    assert incident.notifications == 10


def test_different_services_get_separate_incidents(clock):
    g = grouper(clock)
    assert g.ingest(make_alert(service="order-api")).outcome is Outcome.NEW_INCIDENT
    assert g.ingest(make_alert(service="risk-engine")).outcome is Outcome.NEW_INCIDENT
    assert len(g.list()) == 2


def test_same_service_in_another_environment_is_separate(clock):
    g = grouper(clock)
    g.ingest(make_alert(namespace="staging"))
    assert g.ingest(make_alert(namespace="prod")).outcome is Outcome.NEW_INCIDENT


def test_duplicate_fingerprint_is_ignored(clock):
    g = grouper(clock)
    g.ingest(make_alert())
    clock.advance(5)
    result = g.ingest(make_alert())
    assert result.outcome is Outcome.DUPLICATE
    assert len(result.incident.alerts) == 1
    assert result.incident.last_activity_at == clock.now


def test_open_incident_keeps_collecting_alerts_after_the_window(clock):
    g = grouper(clock)
    first = g.ingest(make_alert(alertname="AvailabilityFast")).incident
    clock.advance(60)  # still firing an hour later: same incident, not a new page
    result = g.ingest(make_alert(alertname="LatencySlow"))
    assert result.outcome is Outcome.JOINED
    assert result.incident is first


def test_resolution_closes_incident_only_when_every_alert_resolved(clock):
    g = grouper(clock)
    g.ingest(make_alert(alertname="A"))
    g.ingest(make_alert(alertname="B"))

    first = g.ingest(make_alert(alertname="A", status="resolved"))
    assert first.outcome is Outcome.RESOLVED
    assert first.incident.status is IncidentStatus.OPEN

    last = g.ingest(make_alert(alertname="B", status="resolved"))
    assert last.incident.status is IncidentStatus.RESOLVED
    assert last.incident.resolved_at == clock.now


def test_flapping_alert_reopens_incident_within_window(clock):
    g = grouper(clock)
    first = g.ingest(make_alert()).incident
    g.ingest(make_alert(status="resolved"))
    clock.advance(10)
    result = g.ingest(make_alert())
    assert result.outcome is Outcome.JOINED
    assert result.incident is first
    assert first.status is IncidentStatus.OPEN
    assert first.resolved_at is None


def test_same_service_after_quiet_window_is_a_new_incident(clock):
    g = grouper(clock)
    g.ingest(make_alert())
    g.ingest(make_alert(status="resolved"))
    clock.advance(20)
    assert g.ingest(make_alert()).outcome is Outcome.NEW_INCIDENT
    assert len(g.list()) == 2


def test_resolved_alert_never_seen_firing_is_ignored(clock):
    g = grouper(clock)
    result = g.ingest(make_alert(status="resolved"))
    assert result.outcome is Outcome.IGNORED
    assert g.list() == []


def test_oldest_incidents_are_evicted_beyond_the_cap(clock):
    g = grouper(clock, max_incidents=2)
    for svc in ["a", "b", "c"]:
        g.ingest(make_alert(service=svc))
        clock.advance(1)
    assert [i.service for i in g.list()] == ["c", "b"]
