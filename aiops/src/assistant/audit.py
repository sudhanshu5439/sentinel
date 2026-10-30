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
            "alerts": [a.model_dump(mode="json") for a in incident.alerts.values()],
            "pack": incident.pack.model_dump(mode="json") if incident.pack else None,
            "result": d.model_dump(mode="json") if d else None,
        },
    }


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


class AuditLog(Protocol):
    async def write(self, incident: Incident, row: dict[str, Any]) -> bool: ...

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
        self.rows.append({"incident_id": incident.id, **row})
        return True

    async def aclose(self) -> None:
        return None
