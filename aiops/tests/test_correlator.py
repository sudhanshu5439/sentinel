from datetime import UTC, datetime, timedelta

from conftest import make_alert

from assistant.correlator import correlate, incident_start
from assistant.evidence import Evidence, EvidenceKind
from assistant.grouping import IncidentGrouper
from assistant.pack import build_pack

START = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
WINDOW = START - timedelta(minutes=30)


def change(kind: str, minutes_before: float, title: str = "", **data) -> Evidence:
    return Evidence(
        kind=EvidenceKind.CHANGE,
        source="kubernetes",
        title=title or f"{kind} change",
        at=START - timedelta(minutes=minutes_before),
        data={"change": kind, **data},
    )


def scores(evidence: list[Evidence]) -> dict[str, float]:
    return {e.title: e.data["suspect_score"] for e in evidence if e.kind is EvidenceKind.CHANGE}


def run(evidence):
    return correlate(evidence, "order-api", "sentinel-staging", START, WINDOW)


def test_a_fresh_revision_of_the_alerting_service_is_the_top_suspect():
    s = scores(
        run(
            [
                change("workload", 4, "order-api revision", owner="order-api"),
                change("workload", 4, "risk-engine revision", owner="risk-engine"),
                change("workload", 28, "older order-api revision", owner="order-api"),
                change("config", 4, "order-api config", name="order-api-faults-abc12"),
            ]
        )
    )
    assert s["order-api revision"] > s["order-api config"] > s["risk-engine revision"]
    assert s["order-api revision"] > s["older order-api revision"]
    assert 0.75 < s["order-api revision"] <= 1.0  # e^(-4/15) ≈ 0.77


def test_a_sync_that_changed_nothing_is_nearly_cleared():
    evidence = run(
        [
            change("sync", 3, "sync that changed nothing", app="sentinel-staging"),
            change("sync", 10, "sync that shipped a revision", app="sentinel-staging"),
            change("workload", 8, "order-api revision", owner="order-api"),
        ]
    )
    s = scores(evidence)
    assert s["sync that changed nothing"] < 0.05
    assert s["sync that shipped a revision"] > s["sync that changed nothing"]
    noop = next(e for e in evidence if e.title == "sync that changed nothing")
    assert "changed no workload or config" in noop.data["reasons"][1]


def test_only_the_latest_sync_before_a_revision_gets_the_credit():
    evidence = run(
        [
            change("sync", 9, "earlier unrelated sync", app="sentinel-staging"),
            change("sync", 7, "sync that shipped it", app="sentinel-staging"),
            change("workload", 6, "order-api revision", owner="order-api"),
        ]
    )
    s = scores(evidence)
    assert s["earlier unrelated sync"] < 0.05
    assert s["sync that shipped it"] > 0.1


def test_a_change_after_the_incident_started_is_treated_as_a_reaction():
    evidence = run([change("workload", -10, "rollback revision", owner="order-api")])
    assert scores(evidence)["rollback revision"] == 0.1
    assert "after the incident started" in evidence[0].data["reasons"][0]


def test_no_change_before_the_incident_is_stated_explicitly():
    evidence = run([change("rollout", 5, "rollout condition", rollout="order-api")])
    note = evidence[-1]
    assert note.title == "no workload or config change before the incident"
    assert "unlikely to be the cause" in note.detail


def test_no_note_when_a_real_change_exists():
    evidence = run([change("config", 5, "config", name="order-api-faults-x")])
    assert all(e.data.get("change") != "none" for e in evidence)


def test_a_change_that_is_no_longer_running_is_history_not_a_suspect():
    """8b live run: an aborted canary (0 replicas) 12 min before a DB outage was blamed."""
    evidence = run(
        [
            change("workload", 12, "aborted canary", owner="order-api", live=False),
            change("config", 12, "its config", name="order-api-faults-x", live=False),
        ]
    )
    s = scores(evidence)
    assert s["aborted canary"] < 0.1 and s["its config"] < 0.1  # 0.2 of e^(-12/15) ≈ 0.09
    assert "no longer running" in evidence[0].data["reasons"]
    note = evidence[-1]
    assert note.title == "no change before the incident is still running"
    assert "cannot be causing the current errors" in note.detail


def test_a_running_change_still_counts_even_next_to_an_aborted_one():
    evidence = run(
        [
            change("workload", 12, "aborted canary", owner="order-api", live=False),
            change("workload", 4, "new release", owner="order-api", live=True),
        ]
    )
    assert all(e.data.get("change") != "none" for e in evidence)
    assert scores(evidence)["new release"] > 0.7


def test_pack_orders_changes_by_suspect_score_and_shows_the_reasons():
    evidence = run(
        [
            change("sync", 3, "noop sync", app="sentinel-staging"),
            change("workload", 6, "order-api revision", owner="order-api"),
        ]
    )
    items = [i for i in build_pack(evidence).items if i.kind is EvidenceKind.CHANGE]
    assert [i.title for i in items] == ["order-api revision", "noop sync"]
    assert items[0].text.startswith("suspect 0.67 (6 min before the incident; order-api itself)")


def test_incident_start_is_the_earliest_alert_start():
    clock = lambda: START  # noqa: E731
    g = IncidentGrouper(window=timedelta(minutes=15), clock=clock)
    incident = g.ingest(make_alert(alertname="A")).incident
    assert incident_start(incident) == datetime(2026, 10, 28, 12, 0, tzinfo=UTC)  # conftest T0
