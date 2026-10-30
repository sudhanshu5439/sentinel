"""Guarded actions (Step 8): from a validated diagnosis to at most one allow-listed action.

The model never chooses an action. It produces a category, a confidence and the evidence IDs it
cites; this module, plain code with unit tests, maps those to the catalogue below. The *target*
of an action (which rollout, which Git commit) comes from the cited items' `facts`, filled in by
the collectors from the Kubernetes and Argo CD APIs, never from model text. So prompt injection
in a log line can at worst change the category, and every category maps to a bounded action.

Tiers (docs/PLAN.md, Flow 2):
  0 notify      a diagnosis to Slack; always
  1 act now     abort a canary in flight; revert the causing commit in staging at high confidence
  2 approve     revert in prod or at medium confidence; scale out: a human clicks Approve
  3 never       deleting volumes, namespaces, anything else: not in the catalogue, so impossible

Then the guards, in order, each able to stop an action (the decision records which one did):
  kill switch   ACTIONS_ENABLED=false in the break-glass ConfigMap
  blast radius  only the incident's own service, only in the allowed namespaces
  rate limit    a few actions per hour overall, one per service per cool-down window
  mode          dry-run (the default) records what *would* be done and does nothing
"""

from collections import deque
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel

from .diagnosis import Category, Confidence, DiagnosisResult, DiagnosisStatus
from .evidence import EvidenceKind
from .pack import EvidencePack

CANARY_IN_FLIGHT = {"Progressing", "Paused"}  # Degraded = already aborted; Healthy = done


class ActionKind(StrEnum):
    NOTIFY = "notify"
    ABORT_CANARY = "abort_canary"  # Argo Rollouts abort: traffic back to the stable version
    REVERT_COMMIT = "revert_commit"  # a revert PR in sentinel-gitops for the causing commit
    SCALE_OUT = "scale_out"  # a PR raising the replica count by one


class Tier(IntEnum):
    NOTIFY = 0
    ACT = 1
    APPROVE = 2


class ActionStatus(StrEnum):
    NOTIFY_ONLY = "notify_only"  # tier 0: nothing to do beyond the message
    DRY_RUN = "dry_run"  # would have acted (tier 1) or asked (tier 2); mode is dry-run
    READY = "ready"  # tier 1, live: cleared every guard, handed to the executor
    AWAITING_APPROVAL = "awaiting_approval"  # tier 2, live: waiting for a human in Slack
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"  # a guard stopped it; `blocked_by` says which


class Decision(BaseModel):
    action: ActionKind
    tier: Tier
    target: dict[str, Any] = {}
    # Why this action and tier: one line per rule that fired, in order. Shown in Slack and kept
    # in the audit log, so a human can check the policy's reasoning, not just its output.
    reasons: list[str]
    evidence: list[str] = []  # the cited IDs the target was taken from
    status: ActionStatus = ActionStatus.NOTIFY_ONLY
    blocked_by: str | None = None
    decided_at: datetime | None = None
    approver: str | None = None
    result: str | None = None


def _notify(*reasons: str) -> Decision:
    return Decision(action=ActionKind.NOTIFY, tier=Tier.NOTIFY, reasons=list(reasons))


def _about_service(facts: dict[str, Any], service: str) -> bool:
    change = facts.get("change")
    if change == "workload":
        return facts.get("owner") == service
    if change == "config":
        return str(facts.get("name", "")).startswith(f"{service}-")
    if change == "rollout":
        return facts.get("rollout") == service
    return change == "sync"  # a sync applies to the whole environment


def decide(
    result: DiagnosisResult | None,
    pack: EvidencePack | None,
    namespace: str,
    service: str,
    prod_namespaces: frozenset[str],
) -> Decision:
    """The policy: diagnosis → one action from the catalogue, with its tier and reasons."""
    if result is None or result.status is not DiagnosisStatus.DIAGNOSED or result.diagnosis is None:
        return _notify("no validated diagnosis: a human takes over, nothing is automated")
    d = result.diagnosis
    if d.category is Category.UNKNOWN or d.confidence is Confidence.LOW:
        return _notify(f"{d.category}/{d.confidence}: not enough evidence to act on")
    if d.category in (Category.DEPENDENCY, Category.RESOURCE):
        return _notify(
            f"{d.category}: rolling back or reverting our own change would not fix it; "
            "evidence goes to a human"
        )

    prod = namespace in prod_namespaces
    reasons = [f"diagnosis {d.category}/{d.confidence}"]
    # Injection in the telemetry does not stop the policy (the target comes from facts, not from
    # text), but nothing runs without a human looking first.
    max_tier = Tier.ACT
    if d.injection_suspected:
        max_tier = Tier.APPROVE
        reasons.append("possible prompt injection in the evidence: a human approves first")

    if d.category is Category.TRAFFIC:
        reasons.append("capacity, not a fault: propose one more replica, a human decides")
        return Decision(
            action=ActionKind.SCALE_OUT,
            tier=Tier.APPROVE,
            target={"namespace": namespace, "service": service, "replicas": "+1"},
            reasons=reasons,
        )

    # bad_release / config_change: validate() guarantees the cause cites a CHANGE item.
    items = {i.id: i for i in pack.items} if pack else {}
    cited = [
        (eid, items[eid].facts)
        for eid in d.probable_cause.evidence
        if eid in items and items[eid].kind is EvidenceKind.CHANGE
    ]
    ours = [(eid, f) for eid, f in cited if _about_service(f, service)]
    if not ours:
        return _notify(*reasons, f"the cited change is not a change to {service}: nothing to undo")

    # A canary still in flight is the cheapest, fastest undo: no Git round-trip.
    for item in pack.items if pack else []:
        f = item.facts
        if (
            f.get("change") == "rollout"
            and f.get("rollout") == service
            and f.get("phase") in CANARY_IN_FLIGHT
            and f.get("canary_hash") != f.get("stable_hash")
        ):
            reasons.append(f"canary of {service} still in flight ({f['phase']}): abort it")
            return Decision(
                action=ActionKind.ABORT_CANARY,
                tier=max_tier,
                target={"namespace": namespace, "rollout": service},
                reasons=reasons,
                evidence=[item.id, *(eid for eid, _ in ours)],
            )

    for eid, f in ours:
        revision = f.get("sync_revision") or (
            f.get("revision") if f.get("change") == "sync" else None
        )
        app = f.get("sync_app") or f.get("app")
        if not revision:
            continue
        tier = max_tier
        if prod:
            tier = Tier.APPROVE
            reasons.append("production: a human approves every change")
        elif d.confidence is not Confidence.HIGH:
            tier = Tier.APPROVE
            reasons.append(f"confidence {d.confidence}: a human approves first")
        reasons.append(
            f"revert gitops commit {str(revision)[:7]} (Argo CD app {app}), cited as {eid}"
        )
        return Decision(
            action=ActionKind.REVERT_COMMIT,
            tier=tier,
            target={"namespace": namespace, "app": app, "revision": revision},
            reasons=reasons,
            evidence=[eid],
        )
    return _notify(*reasons, "no Git commit is linked to the cited change: nothing to revert")


class Guards:
    """Kill switch, blast radius and rate limits, applied to a decision before anything runs.
    In memory: a restart resets the counters, which errs towards *fewer* actions only if the
    pod does not restart in a loop; the audit log is the durable record."""

    def __init__(
        self,
        allowed_namespaces: frozenset[str],
        per_hour: int,
        cooldown: timedelta,
    ) -> None:
        self._allowed, self._per_hour, self._cooldown = allowed_namespaces, per_hour, cooldown
        self._recent: deque[datetime] = deque()
        self._last_by_service: dict[tuple[str, str], datetime] = {}

    def check(
        self, decision: Decision, namespace: str, service: str, actions_enabled: bool, now: datetime
    ) -> str | None:
        """None if the action may proceed, else the name of the guard that stops it."""
        if decision.action is ActionKind.NOTIFY:
            return None
        if not actions_enabled:
            return "kill switch: ACTIONS_ENABLED=false"
        if namespace not in self._allowed:
            return f"blast radius: {namespace} is not an allowed namespace"
        target = decision.target
        if (
            target.get("namespace") != namespace
            or target.get("service", target.get("rollout", service)) != service
        ):
            return "blast radius: the target is not the incident's own service"
        while self._recent and now - self._recent[0] > timedelta(hours=1):
            self._recent.popleft()
        if len(self._recent) >= self._per_hour:
            return f"rate limit: {self._per_hour} actions in the last hour"
        last = self._last_by_service.get((namespace, service))
        if last is not None and now - last < self._cooldown:
            minutes = int((now - last).total_seconds() // 60)
            return f"rate limit: {service} had an action {minutes} min ago"
        return None

    def record(self, namespace: str, service: str, now: datetime) -> None:
        self._recent.append(now)
        self._last_by_service[(namespace, service)] = now


def apply_guards(
    decision: Decision,
    guards: Guards,
    namespace: str,
    service: str,
    actions_enabled: bool,
    dry_run: bool,
    now: datetime | None = None,
) -> Decision:
    """Set the decision's status: blocked by a guard, dry run, or ready (act / await approval).
    A dry run counts against the rate limit too, so dry-run behaviour matches live behaviour."""
    now = now or datetime.now(UTC)
    decision.decided_at = now
    if decision.action is ActionKind.NOTIFY:
        decision.status = ActionStatus.NOTIFY_ONLY
        return decision
    blocked = guards.check(decision, namespace, service, actions_enabled, now)
    if blocked:
        decision.status, decision.blocked_by = ActionStatus.BLOCKED, blocked
        return decision
    guards.record(namespace, service, now)
    if dry_run:
        decision.status = ActionStatus.DRY_RUN
    elif decision.tier is Tier.APPROVE:
        decision.status = ActionStatus.AWAITING_APPROVAL
    else:
        decision.status = ActionStatus.READY
    return decision
