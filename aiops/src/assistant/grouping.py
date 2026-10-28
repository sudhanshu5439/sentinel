"""Alert grouping: many alerts become one incident (hard problem #6, alert storms).

Rules:
- Key: (namespace, service). namespace is the environment (staging, prod); service comes from the
  SLO rule's `service` label. Fast and slow burn, availability and latency alerts for order-api in
  staging therefore all land in one incident.
- An alert joins the latest incident with its key if that incident is still open, or if it had
  activity within the window. A flapping alert re-opens its incident instead of paging twice.
- Otherwise it opens a new incident. Only NEW_INCIDENT starts the diagnosis pipeline, so one storm
  costs one diagnosis and one LLM call.
- Same fingerprint and same status is a duplicate: Alertmanager re-sends firing alerts on every
  group update and repeat interval.
- A resolved alert we never saw firing (for example after a restart) is ignored.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .models import Alert, AlertRecord, Incident, IncidentStatus


class Outcome(StrEnum):
    NEW_INCIDENT = "new_incident"
    JOINED = "joined"  # a new (or re-fired) alert added to an existing incident
    DUPLICATE = "duplicate"
    RESOLVED = "resolved"  # one alert resolved; the incident may still be open
    IGNORED = "ignored"


@dataclass(frozen=True)
class GroupResult:
    outcome: Outcome
    incident: Incident | None


def incident_key(labels: dict[str, str]) -> tuple[str, str]:
    namespace = labels.get("namespace", "unknown")
    service = labels.get("service") or labels.get("job") or labels.get("alertname", "unknown")
    return namespace, service


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _record(alert: Alert) -> AlertRecord:
    return AlertRecord(
        fingerprint=alert.fingerprint,
        alertname=alert.labels.get("alertname", "unknown"),
        severity=alert.labels.get("severity", "unknown"),
        status=alert.status,
        starts_at=alert.starts_at,
        summary=alert.annotations.get("summary", ""),
        labels=alert.labels,
    )


class IncidentGrouper:
    """In-memory incident store. Not thread-safe: call it only from the event loop."""

    def __init__(
        self,
        window: timedelta,
        max_incidents: int = 500,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._window = window
        self._max = max_incidents
        self._clock = clock
        self._incidents: dict[str, Incident] = {}  # insertion order = opening order
        self._latest: dict[tuple[str, str], str] = {}  # key -> id of its newest incident

    def ingest(self, alert: Alert) -> GroupResult:
        now = self._clock()
        key = incident_key(alert.labels)
        incident = self._current(key, now)

        if alert.status == "resolved":
            return self._resolve(incident, alert, now)
        if incident is None:
            return GroupResult(Outcome.NEW_INCIDENT, self._open(key, alert, now))

        incident.notifications += 1
        incident.last_activity_at = now
        known = incident.alerts.get(alert.fingerprint)
        if known is not None and known.status == "firing":
            return GroupResult(Outcome.DUPLICATE, incident)
        incident.alerts[alert.fingerprint] = _record(alert)
        incident.status = IncidentStatus.OPEN
        incident.resolved_at = None
        return GroupResult(Outcome.JOINED, incident)

    def list(self) -> list[Incident]:
        return list(reversed(self._incidents.values()))  # newest first

    def get(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def _current(self, key: tuple[str, str], now: datetime) -> Incident | None:
        incident_id = self._latest.get(key)
        incident = self._incidents.get(incident_id) if incident_id else None
        if incident is None:  # never seen, or evicted
            return None
        recently_active = now - incident.last_activity_at <= self._window
        if incident.status is IncidentStatus.OPEN or recently_active:
            return incident
        return None

    def _open(self, key: tuple[str, str], alert: Alert, now: datetime) -> Incident:
        namespace, service = key
        incident = Incident(
            id=f"{now:%Y%m%dT%H%M%SZ}-{namespace}-{service}",
            namespace=namespace,
            service=service,
            status=IncidentStatus.OPEN,
            opened_at=now,
            last_activity_at=now,
            alerts={alert.fingerprint: _record(alert)},
            notifications=1,
        )
        self._incidents[incident.id] = incident
        self._latest[key] = incident.id
        while len(self._incidents) > self._max:
            self._incidents.pop(next(iter(self._incidents)))
        return incident

    def _resolve(self, incident: Incident | None, alert: Alert, now: datetime) -> GroupResult:
        record = incident.alerts.get(alert.fingerprint) if incident else None
        if incident is None or record is None:
            return GroupResult(Outcome.IGNORED, None)

        incident.notifications += 1
        if record.status == "resolved":
            return GroupResult(Outcome.DUPLICATE, incident)
        record.status = "resolved"
        record.ends_at = alert.ends_at
        incident.last_activity_at = now
        if all(r.status == "resolved" for r in incident.alerts.values()):
            incident.status = IncidentStatus.RESOLVED
            incident.resolved_at = now
        return GroupResult(Outcome.RESOLVED, incident)
