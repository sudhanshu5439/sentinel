"""Audit log (Step 8): every diagnosis and every decision, durably, in PostgreSQL.

What a reviewer needs to explain any automated step after the fact:
  diagnosis  the evidence pack exactly as the model saw it (and its SHA-256), the prompt's
             SHA-256, the model's validated answer or the reasons it was rejected, tokens, cost
  decision   the action chosen by policy code, its tier, the reasons, the guard that stopped it
  approval   who approved or rejected, when (8c)
  action     what was executed and what came back (8d)

Append-only by construction, not by convention: the assistant's database role may INSERT and
SELECT, nothing else, and a trigger rejects UPDATE, DELETE and TRUNCATE even for the owner
(schema: sentinel-gitops platform/aiops/audit-db.yaml). Grafana reads it with a SELECT-only role.

Rule: no audit, no action. If a decision cannot be written, a live action is blocked; the
investigation itself (diagnosis, Slack) carries on, because telling a human is always safe.
"""

import hashlib
import json
import logging
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .actions import Decision
from .models import Incident

log = logging.getLogger("assistant.audit")

INSERT = """
INSERT INTO audit_events
  (incident_id, namespace, service, event, category, confidence, action, tier, status,
   prompt_sha256, pack_sha256, cost_usd, payload)
VALUES (%(incident_id)s, %(namespace)s, %(service)s, %(event)s, %(category)s, %(confidence)s,
        %(action)s, %(tier)s, %(status)s, %(prompt_sha256)s, %(pack_sha256)s, %(cost_usd)s,
        %(payload)s)
"""


def pack_sha256(incident: Incident) -> str | None:
    if incident.pack is None:
        return None
    return hashlib.sha256(incident.pack.render().encode()).hexdigest()


def diagnosis_row(incident: Incident) -> dict[str, Any]:
    d = incident.diagnosis
    diagnosis = d.diagnosis if d else None
    return {
        "event": "diagnosis",
        "category": str(diagnosis.category) if diagnosis else None,
        "confidence": str(diagnosis.confidence) if diagnosis else None,
        "status": str(d.status) if d else None,
        "prompt_sha256": d.prompt_sha256 if d else None,
        "pack_sha256": pack_sha256(incident),
        "cost_usd": d.cost_usd if d else None,
        "payload": {
            "incident": _meta(incident),
            "alerts": [a.model_dump(mode="json") for a in incident.alerts.values()],
            "pack": incident.pack.model_dump(mode="json") if incident.pack else None,
            "result": d.model_dump(mode="json") if d else None,
        },
    }


def opened_row(incident: Incident) -> dict[str, Any]:
    """Written before Alertmanager gets its 200: Alertmanager will not re-send a delivered
    alert for 12 h, so an incident accepted but not yet diagnosed when the pod stops would be
    lost (Step 9 pilot: a deploy killed the assistant 60 s into a DB-outage investigation).
    On start-up, an incident with this row and no diagnosis is investigated again."""
    return {
        "event": "opened",
        "category": None,
        "confidence": None,
        "status": "open",
        "prompt_sha256": None,
        "pack_sha256": None,
        "cost_usd": None,
        "payload": {
            "incident": _meta(incident),
            "alerts": [a.model_dump(mode="json") for a in incident.alerts.values()],
        },
    }


def resolved_row(incident: Incident) -> dict[str, Any]:
    """Every alert of the incident resolved. Also what tells a restarted assistant that this
    incident is over (incidents are rebuilt from the audit log, see restore.py)."""
    return {
        "event": "resolved",
        "category": None,
        "confidence": None,
        "status": "resolved",
        "prompt_sha256": None,
        "pack_sha256": None,
        "cost_usd": None,
        "payload": {
            "incident": _meta(incident),
            "alerts": [a.model_dump(mode="json") for a in incident.alerts.values()],
        },
    }


def verify_row(incident: Incident) -> dict[str, Any]:
    """8e: what the SLI said after the action. status: recovered | not_recovered | unknown."""
    v = incident.verification
    status = (
        "unknown"
        if v is None or v.recovered is None
        else ("recovered" if v.recovered else "not_recovered")
    )
    decision = incident.decision
    return {
        "event": "verify",
        "category": None,
        "confidence": None,
        "action": str(decision.action) if decision else None,
        "tier": int(decision.tier) if decision else None,
        "status": status,
        "prompt_sha256": None,
        "pack_sha256": None,
        "cost_usd": None,
        "payload": v.model_dump(mode="json") if v else {},
    }


def _meta(incident: Incident) -> dict[str, Any]:
    return incident.model_dump(
        mode="json",
        include={"opened_at", "last_activity_at", "resolved_at", "notifications", "notified_at"},
    )


def decision_row(incident: Incident, decision: Decision, event: str = "decision") -> dict[str, Any]:
    d = incident.diagnosis.diagnosis if incident.diagnosis else None
    return {
        "event": event,
        "category": str(d.category) if d else None,
        "confidence": str(d.confidence) if d else None,
        "action": str(decision.action),
        "tier": int(decision.tier),
        "status": str(decision.status),
        "prompt_sha256": incident.diagnosis.prompt_sha256 if incident.diagnosis else None,
        "pack_sha256": pack_sha256(incident),
        "cost_usd": None,
        "payload": decision.model_dump(mode="json"),
    }


RESTORE_EVENTS = ("opened", "diagnosis", "decision", "action", "approval", "resolved")
RECENT = """
SELECT incident_id, namespace, service, event, at, payload FROM audit_events
WHERE at > now() - make_interval(hours => %(hours)s)
  AND event IN ('opened', 'diagnosis', 'decision', 'action', 'approval', 'resolved')
ORDER BY id
"""


class AuditLog(Protocol):
    async def write(self, incident: Incident, row: dict[str, Any]) -> bool: ...

    async def recent(self, hours: int) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


class PostgresAudit:
    def __init__(self, dsn: str, timeout_seconds: float = 5.0) -> None:
        # open=False: the pool connects lazily, so a database that is down at start-up does not
        # stop the assistant from starting (and diagnosing); writes fail and say so instead.
        self._pool = AsyncConnectionPool(
            dsn, min_size=0, max_size=2, open=False, timeout=timeout_seconds
        )
        self._opened = False

    async def write(self, incident: Incident, row: dict[str, Any]) -> bool:
        params = {
            "incident_id": incident.id,
            "namespace": incident.namespace,
            "service": incident.service,
            "action": None,
            "tier": None,
            **row,
            "payload": Jsonb(row["payload"], dumps=lambda o: json.dumps(o, default=str)),
        }
        try:
            if not self._opened:
                await self._pool.open()
                self._opened = True
            async with self._pool.connection() as conn:
                await conn.execute(INSERT, params)
            return True
        except Exception as exc:  # noqa: BLE001 - reported, and blocks live actions upstream
            log.error("audit write failed for incident %s (%s): %r", incident.id, row["event"], exc)
            return False

    async def recent(self, hours: int) -> list[dict[str, Any]]:
        """Rows of the last `hours`, oldest first. [] (and a log line) if the DB is down: the
        assistant then starts empty, as it did before the audit log existed."""
        try:
            if not self._opened:
                await self._pool.open()
                self._opened = True
            async with self._pool.connection() as conn:
                cur = await conn.execute(RECENT, {"hours": hours})
                cols = [c.name for c in cur.description or []]
                return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
            log.error("audit read failed, starting without restored incidents: %r", exc)
            return []

    async def aclose(self) -> None:
        if self._opened:
            await self._pool.close()


class MemoryAudit:
    """For tests and local runs without a database: same interface, a list."""

    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = fail

    async def write(self, incident: Incident, row: dict[str, Any]) -> bool:
        if self.fail:
            return False
        self.rows.append(
            {
                "incident_id": incident.id,
                "namespace": incident.namespace,
                "service": incident.service,
                **row,
            }
        )
        return True

    async def recent(self, hours: int) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["event"] in RESTORE_EVENTS]

    async def aclose(self) -> None:
        return None
