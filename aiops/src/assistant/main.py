"""sentinel-assistant: the read-only incident assistant.

POST /webhook/alertmanager  -> group alerts into incidents (diagnosis pipeline comes next)
GET  /incidents, GET /incidents/{id}
/healthz (process alive), /readyz (ready to receive alerts)
"""

import json
import logging
import os
import sys
from datetime import timedelta

from fastapi import FastAPI, HTTPException

from .config import Settings
from .grouping import IncidentGrouper, Outcome
from .models import Incident, WebhookPayload

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


def create_app(settings: Settings | None = None, grouper: IncidentGrouper | None = None) -> FastAPI:
    """App factory: real dependencies by default, fakes injected by tests."""
    settings = settings or Settings.from_env()
    setup_logging(settings.service_name, settings.service_version)
    grouper = grouper or IncidentGrouper(
        window=timedelta(minutes=settings.group_window_minutes),
        max_incidents=settings.max_incidents,
    )

    app = FastAPI(title="sentinel-assistant", version=settings.service_version)

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
