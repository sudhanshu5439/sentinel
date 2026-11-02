"""Rebuild incidents from the audit log after a restart.

Without this, a restart (every deploy) forgot every incident: the "resolved" notification that
followed was ignored, and a still-firing alert re-sent by Alertmanager looked new and was
investigated (and paid for) a second time. Seen live in 8b run 3.

The audit log already holds what an incident is made of: the `diagnosis` row (alerts, evidence
pack, diagnosis), the latest `decision`/`action`/`approval` row (the decision) and a `resolved`
row once every alert has resolved. Only the recent past is restored; anything older than the
lookback is history, not state.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .actions import Decision
from .diagnosis import DiagnosisResult
from .grouping import IncidentGrouper
from .models import AlertRecord, Incident, IncidentStatus
from .pack import EvidencePack

log = logging.getLogger("assistant.restore")


def _opened_at(incident_id: str) -> datetime | None:
    """Rows written before the incident meta existed: the id starts 20261030T122640Z-..."""
    try:
        return datetime.strptime(incident_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def rebuild(rows: list[dict[str, Any]]) -> list[Incident]:
    """Rows oldest first → incidents oldest first. Rows that do not parse are skipped (and
    logged): a bad row must not stop the assistant from starting."""
    incidents: dict[str, Incident] = {}
    for row in rows:
        iid, event, payload = row["incident_id"], row["event"], row.get("payload") or {}
        try:
            if event == "opened" and iid not in incidents:
                # Accepted but maybe never diagnosed: kept open with no diagnosis, so start-up
                # investigates it again (a later diagnosis row replaces this one).
                meta = payload.get("incident") or {}
                alerts = [AlertRecord.model_validate(a) for a in payload.get("alerts", [])]
                opened = meta.get("opened_at") or _opened_at(iid)
                if opened is None:
                    continue
                incidents[iid] = Incident(
                    id=iid,
                    namespace=row["namespace"],
                    service=row["service"],
                    status=IncidentStatus.OPEN,
                    opened_at=opened,
                    last_activity_at=meta.get("last_activity_at") or opened,
                    alerts={a.fingerprint: a for a in alerts},
                )
            elif event == "diagnosis":
                meta = payload.get("incident") or {}
                opened = meta.get("opened_at") or _opened_at(iid)
                if opened is None:
                    continue
                alerts = [AlertRecord.model_validate(a) for a in payload.get("alerts", [])]
                incident = Incident(
                    id=iid,
                    namespace=row["namespace"],
                    service=row["service"],
                    status=IncidentStatus.OPEN,
                    opened_at=opened,
                    last_activity_at=meta.get("last_activity_at") or opened,
                    notifications=meta.get("notifications", 1),
                    notified_at=meta.get("notified_at"),
                    alerts={a.fingerprint: a for a in alerts},
                    pack=EvidencePack.model_validate(payload["pack"])
                    if payload.get("pack")
                    else None,
                    diagnosis=DiagnosisResult.model_validate(payload["result"])
                    if payload.get("result")
                    else None,
                    audited=True,
                )
                incidents[iid] = incident
            elif iid in incidents and event in ("decision", "action", "approval"):
                incidents[iid].decision = Decision.model_validate(payload)
            elif iid in incidents and event == "resolved":
                # Rebuilt through model_validate, not by attribute assignment: pydantic does
                # not validate assignments, so a JSON timestamp would stay a str and the next
                # alert for this service crashed the webhook (found by the Step 9 pilot).
                incident = incidents[iid]
                meta = payload.get("incident") or {}
                alerts = dict(incident.alerts)
                for a in payload.get("alerts", []):
                    record = AlertRecord.model_validate(a)
                    alerts[record.fingerprint] = record
                incidents[iid] = Incident.model_validate(
                    {
                        **dict(incident),
                        "alerts": alerts,
                        "status": IncidentStatus.RESOLVED,
                        "resolved_at": meta.get("resolved_at") or row.get("at"),
                        "last_activity_at": meta.get("last_activity_at")
                        or incident.last_activity_at,
                    }
                )
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            log.warning("audit row for %s (%s) not restored: %r", iid, event, exc)
    return list(incidents.values())


def restore(rows: list[dict[str, Any]], grouper: IncidentGrouper) -> list[Incident]:
    incidents = rebuild(rows)
    for incident in incidents:
        grouper.restore(incident)
    if incidents:
        open_ = sum(i.status is IncidentStatus.OPEN for i in incidents)
        log.info("restored %d incidents from the audit log (%d open)", len(incidents), open_)
    return incidents
