"""sentinel-assistant: the incident assistant.

POST /webhook/alertmanager  -> group alerts into incidents; a NEW incident starts one
                               investigation in the background (collect, correlate, pack,
                               diagnose, decide by policy, audit, act or plan, notify)
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

import httpx
from fastapi import FastAPI, HTTPException

from .actions import ActionKind, ActionStatus, Guards, apply_guards, decide
from .audit import AuditLog, PostgresAudit, decision_row, diagnosis_row
from .collectors import Collector, build_collectors, collect_all, kube_client
from .config import Settings
from .correlator import correlate, incident_start
from .diagnosis import DiagnosisResult, DiagnosisStatus, diagnose
from .evidence import Scope
from .executor import Executor, GitOpsRepo, RolloutsApi
from .grouping import IncidentGrouper, Outcome
from .llm import LLM, AnthropicLLM
from .models import Incident, WebhookPayload
from .notify import SlackNotifier
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
    # The Anthropic SDK uses httpx2, which logs the same way.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def investigate(
    incident: Incident,
    collectors: list[Collector],
    settings: Settings,
    llm: LLM | None = None,
    notifier: SlackNotifier | None = None,
    guards: Guards | None = None,
    audit: AuditLog | None = None,
    executor: Executor | None = None,
) -> None:
    """Collect evidence, score changes, build the pack, ask the LLM (once per incident), decide
    the action by policy, audit, execute (or plan, in dry-run), notify, await approval."""
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
    if settings.kill_switch_engaged():
        incident.diagnosis = DiagnosisResult(
            status=DiagnosisStatus.SKIPPED,
            errors=["LLM disabled by the kill switch"],
            finished_at=datetime.now(UTC),
        )
    else:
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
    await decide_and_audit(incident, settings, guards, audit)
    decision = incident.decision
    if executor is not None and decision is not None and decision.action is not ActionKind.NOTIFY:
        if decision.status is not ActionStatus.BLOCKED:
            await executor.run(decision, incident.id, dry_run=not settings.action_mode_live)
            log.info(
                "incident %s: %s %s: %s",
                incident.id,
                decision.action,
                decision.status,
                decision.result,
            )
            if audit is not None:
                await audit.write(incident, decision_row(incident, decision, event="action"))
    if notifier is not None:
        incident.notify_error = await notifier.send(incident)
        if incident.notify_error is None:
            incident.notified_at = datetime.now(UTC)
            log.info("incident %s: posted to Slack", incident.id)
    # Tier 2: the PR is open; a human merging it is the approval. This task waits for it.
    if executor is not None and decision is not None:
        changed = await executor.await_approval(
            decision, settings.approval_poll_seconds, settings.approval_timeout_minutes * 60
        )
        if changed:
            log.info("incident %s: approval %s: %s", incident.id, decision.status, decision.result)
            if audit is not None:
                await audit.write(incident, decision_row(incident, decision, event="approval"))
            if notifier is not None:
                await notifier.send_update(incident, decision.result or "")


LIVE = (ActionStatus.READY, ActionStatus.AWAITING_APPROVAL)


async def decide_and_audit(
    incident: Incident, settings: Settings, guards: Guards | None, audit: AuditLog | None
) -> None:
    """Policy, then guards, then the audit log. No audit, no action: a live action whose
    record cannot be written is blocked (dry runs change nothing, so they may proceed)."""
    decision = decide(
        incident.diagnosis,
        incident.pack,
        incident.namespace,
        incident.service,
        settings.prod_namespaces,
    )
    guards = guards or Guards(
        settings.allowed_namespaces,
        settings.actions_per_hour,
        timedelta(minutes=settings.action_cooldown_minutes),
    )
    apply_guards(
        decision,
        guards,
        incident.namespace,
        incident.service,
        settings.actions_enabled(),
        dry_run=not settings.action_mode_live,
    )

    def block_if_live() -> None:
        if decision.status in LIVE:
            decision.status = ActionStatus.BLOCKED
            decision.blocked_by = "no audit, no action: the audit log could not be written"

    ok = audit is not None and await audit.write(incident, diagnosis_row(incident))
    if not ok:
        block_if_live()
    if audit is not None:
        ok = await audit.write(incident, decision_row(incident, decision)) and ok
        if not ok:
            block_if_live()
    incident.decision = decision
    incident.audited = ok if audit is not None else None
    log.info(
        "incident %s: decision %s tier %d %s%s",
        incident.id,
        decision.action,
        decision.tier,
        decision.status,
        f" ({decision.blocked_by})" if decision.blocked_by else "",
    )


def _log_failure(task: asyncio.Task[None]) -> None:
    """A background investigation that crashes must say so: nobody awaits these tasks."""
    if not task.cancelled() and task.exception() is not None:
        log.error("investigation failed", exc_info=task.exception())


def create_app(
    settings: Settings | None = None,
    grouper: IncidentGrouper | None = None,
    collectors: list[Collector] | None = None,
    llm: LLM | None = None,
    notifier: SlackNotifier | None = None,
    audit: AuditLog | None = None,
    executor: Executor | None = None,
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
    if notifier is None and settings.slack_webhook_url:
        notifier = SlackNotifier(settings.slack_webhook_url)
        clients.append(notifier)
    if audit is None and settings.audit_database_url:
        audit = PostgresAudit(settings.audit_database_url)
        clients.append(audit)
    if executor is None and settings.kube_api_url:  # in-cluster (or against kubectl proxy)
        github = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        kube = kube_client(settings)
        clients += [github] + ([kube] if kube else [])
        executor = Executor(
            GitOpsRepo(github, settings.gitops_repo, settings.github_token),
            RolloutsApi(kube, settings.kube_api_url) if kube else None,
        )
    # One set of guards for the process: rate limits count across incidents.
    guards = Guards(
        settings.allowed_namespaces,
        settings.actions_per_hour,
        timedelta(minutes=settings.action_cooldown_minutes),
    )
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
                task = asyncio.create_task(
                    investigate(
                        result.incident,
                        collectors,
                        settings,
                        llm,
                        notifier,
                        guards,
                        audit,
                        executor,
                    )
                )
                running.add(task)
                task.add_done_callback(running.discard)
                task.add_done_callback(_log_failure)
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
