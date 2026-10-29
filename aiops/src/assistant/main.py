"""sentinel-assistant: the read-only incident assistant.

POST /webhook/alertmanager  -> group alerts into incidents; a NEW incident starts one
                               investigation in the background (collect, correlate, pack, diagnose)
GET  /incidents, GET /incidents/{id}
/healthz (process alive), /readyz (ready to receive alerts)
"""

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException

from .collectors import Collector, build_collectors, collect_all
from .config import Settings
from .correlator import correlate, incident_start
from .diagnosis import diagnose
from .evidence import Scope
from .grouping import IncidentGrouper, Outcome
from .llm import LLM, AnthropicLLM
from .models import Incident, WebhookPayload
from .pack import build_pack

log = logging.getLogger("assistant")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str) -> None:
        super().__init__()
        self._service, self._version = service, version

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "service": self._service,
            "version": self._version,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(service: str, version: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, version))
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    # httpx logs every request at INFO: one line per Prometheus query per incident is noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def investigate(
    incident: Incident, collectors: list[Collector], settings: Settings, llm: LLM | None = None
) -> None:
    """Collect evidence, score changes, build the pack, then ask the LLM (once per incident)."""
    now = datetime.now(UTC)
    try:
        scope = Scope(
            namespace=incident.namespace,
            service=incident.service,
            start=incident.opened_at - timedelta(minutes=settings.lookback_minutes),
            end=now,
        )
    except ValueError as exc:
        log.warning("incident %s: not investigated: %s", incident.id, exc)
        return
    evidence = await collect_all(scope, collectors, settings.collect_timeout_seconds)
    incident.evidence = correlate(
        evidence, incident.service, incident.namespace, incident_start(incident), scope.start
    )
    incident.evidence_collected_at = datetime.now(UTC)
    incident.pack = build_pack(incident.evidence, settings.evidence_token_budget)
    log.info(
        "incident %s: %d evidence items -> pack of %d (~%d tokens, dropped %s)",
        incident.id,
        len(incident.evidence),
        len(incident.pack.items),
        incident.pack.tokens_estimated,
        incident.pack.dropped or "none",
    )
    alerts = [(a.alertname, a.severity, a.summary) for a in incident.alerts.values()]
    incident.diagnosis = await diagnose(
        llm, alerts, incident.pack, settings.llm_budget_usd, settings.llm_max_tokens
    )
    d = incident.diagnosis
    log.info(
        "incident %s: diagnosis %s%s (%d attempts, %d+%d tokens, $%.4f)%s",
        incident.id,
        d.status,
        f" {d.diagnosis.category}/{d.diagnosis.confidence}" if d.diagnosis else "",
        d.attempts,
        d.input_tokens,
        d.output_tokens,
        d.cost_usd,
        f": {'; '.join(d.errors)}" if d.errors else "",
    )


def create_app(
    settings: Settings | None = None,
    grouper: IncidentGrouper | None = None,
    collectors: list[Collector] | None = None,
    llm: LLM | None = None,
) -> FastAPI:
    """App factory: real dependencies by default, fakes injected by tests."""
    settings = settings or Settings.from_env()
    setup_logging(settings.service_name, settings.service_version)
    grouper = grouper or IncidentGrouper(
        window=timedelta(minutes=settings.group_window_minutes),
        max_incidents=settings.max_incidents,
    )
    clients = []
    if collectors is None:
        collectors, clients = build_collectors(settings)
    if llm is None and settings.llm_enabled:
        llm = AnthropicLLM(settings.llm_model, settings.llm_timeout_seconds)
        clients.append(llm)
    # Strong references: the event loop only keeps weak ones, so an unreferenced task can vanish.
    running: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for client in clients:
            await client.aclose()

    app = FastAPI(title="sentinel-assistant", version=settings.service_version, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # No dependencies yet; Prometheus/Loki/LLM reachability checks join when they are used.
        return {"status": "ready"}

    # async def on purpose: handlers run one at a time on the event loop, so the in-memory
    # grouper needs no lock. A sync def would run in a thread pool and race.
    @app.post("/webhook/alertmanager")
    async def alertmanager_webhook(payload: WebhookPayload) -> dict[str, object]:
        new_incidents: list[str] = []
        for alert in payload.alerts:
            result = grouper.ingest(alert)
            if result.outcome is Outcome.NEW_INCIDENT and result.incident is not None:
                new_incidents.append(result.incident.id)
                log.info("incident opened: %s", result.incident.id)
                # Answer Alertmanager now; investigating takes seconds and must not block it.
                task = asyncio.create_task(investigate(result.incident, collectors, settings, llm))
                running.add(task)
                task.add_done_callback(running.discard)
            else:
                log.info(
                    "alert %s %s: %s",
                    alert.labels.get("alertname", "?"),
                    alert.status,
                    result.outcome,
                )
        return {"received": len(payload.alerts), "new_incidents": new_incidents}

    @app.get("/incidents")
    async def list_incidents() -> list[Incident]:
        return grouper.list()

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> Incident:
        incident = grouper.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return incident

    return app


def app() -> FastAPI:  # `uvicorn assistant.main:app --factory`
    return create_app()
