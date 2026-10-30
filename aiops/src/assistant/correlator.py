"""Change correlation (7d): which recent change most plausibly caused this incident?

Each change gets a suspect score in [0, 1] = time × service × kind, with the reasons spelled out,
because the diagnosis (and a human) must be able to see *why* something is a suspect:

- time     exponential decay with the minutes between the change and the incident start
           (15-minute time constant); a change made *after* the incident started is most likely a
           reaction to it (a rollback, an abort), not its cause.
- service  the alerting service itself: 1.0; another service in the same namespace (a
           dependency): 0.5.
- kind     a new workload revision (new ReplicaSet = new image or config): 1.0; a new ConfigMap
           version: 0.9; a rollout status change: 0.3 (a symptom of a release, not a separate
           cause); an Argo CD sync: 0.3 if it produced a revision or config in that namespace,
           0.05 if it changed nothing there (any commit anywhere in the repo creates a sync).

If no workload or config changed before the incident, the correlator says so explicitly. That
negative evidence is what lets the diagnosis answer "not the deploy" for a database outage, a
network fault or a traffic surge.
"""

import math
from datetime import datetime, timedelta

from .evidence import Evidence, EvidenceKind
from .models import Incident

TIME_CONSTANT_MINUTES = 15.0
AFTER_START_GRACE = timedelta(minutes=2)  # clock skew and alert `for:` delays
SYNC_EFFECT_WINDOW = (timedelta(minutes=-1), timedelta(minutes=10))
KIND_WEIGHT = {"workload": 1.0, "config": 0.9, "rollout": 0.3}
SYNC_WEIGHT_CHANGED, SYNC_WEIGHT_NOOP = 0.3, 0.05
NEIGHBOUR = 0.5


def incident_start(incident: Incident) -> datetime:
    """When the problem began: the earliest `startsAt` among its alerts (Alertmanager sends the
    moment the rule started firing), not when the webhook reached us."""
    starts = [a.starts_at for a in incident.alerts.values() if a.starts_at.year > 1]
    return min(starts, default=incident.opened_at)


def _time_factor(at: datetime, start: datetime) -> tuple[float, str]:
    minutes = (start - at).total_seconds() / 60
    if at > start + AFTER_START_GRACE:
        return 0.1, f"{-minutes:.0f} min after the incident started (likely a reaction)"
    minutes = max(minutes, 0.0)
    return math.exp(-minutes / TIME_CONSTANT_MINUTES), f"{minutes:.0f} min before the incident"


def _touches(e: Evidence, service: str) -> bool:
    change = e.data.get("change")
    if change == "workload":
        return e.data.get("owner") == service
    if change == "config":
        return str(e.data.get("name", "")).startswith(f"{service}-")
    if change == "rollout":
        return e.data.get("rollout") == service
    return True  # a sync applies to the whole environment


def correlate(
    evidence: list[Evidence], service: str, namespace: str, start: datetime, window_start: datetime
) -> list[Evidence]:
    """Score every CHANGE in place and append a 'no change' note when nothing changed before
    the incident. Returns the same list for convenience."""
    changes = [e for e in evidence if e.kind is EvidenceKind.CHANGE and e.at is not None]
    real = [e for e in changes if e.data.get("change") in ("workload", "config")]
    syncs = sorted((e for e in changes if e.data.get("change") == "sync"), key=lambda e: e.at)

    # Each real change belongs to the *latest* sync that could have caused it, not to every sync
    # nearby: an earlier, unrelated commit must not get the credit (found live: an observability
    # PR synced 6 min earlier was blamed for a bad release).
    lo, hi = SYNC_EFFECT_WINDOW
    effects: dict[int, list[str]] = {}
    for r in real:
        causes = [s for s in syncs if s.at + lo <= r.at <= s.at + hi]
        if causes:
            effects.setdefault(id(causes[-1]), []).append(r.title)
            # The Git commit that produced this change: what a revert PR (Step 8) would undo.
            r.data["sync_app"] = causes[-1].data.get("app")
            r.data["sync_revision"] = causes[-1].data.get("revision")

    for e in changes:
        change = e.data.get("change")
        t, when = _time_factor(e.at, start)
        reasons = [when]
        if change == "sync":
            produced = effects.get(id(e), [])
            kind = SYNC_WEIGHT_CHANGED if produced else SYNC_WEIGHT_NOOP
            reasons.append(
                f"produced: {'; '.join(produced)[:120]}"
                if produced
                else f"changed no workload or config in {namespace} (Git moved elsewhere)"
            )
        else:
            kind = KIND_WEIGHT.get(str(change), 0.3)
        s = 1.0 if _touches(e, service) else NEIGHBOUR
        if change != "sync":
            reasons.append(f"{service} itself" if s == 1.0 else f"another service, not {service}")
        e.data["suspect_score"] = round(t * s * kind, 2)
        e.data["reasons"] = reasons

    if not any(r.at <= start + AFTER_START_GRACE for r in real):
        evidence.append(
            Evidence(
                kind=EvidenceKind.CHANGE,
                source="correlator",
                title="no workload or config change before the incident",
                detail=(
                    f"No new ReplicaSet or ConfigMap in {namespace} between "
                    f"{window_start:%H:%M} and {start:%H:%M} UTC: a deploy or config change is "
                    "unlikely to be the cause; look at dependencies, infrastructure and load."
                ),
                at=start,
                data={"change": "none", "summary": True, "suspect_score": 0.0, "reasons": []},
            )
        )
    return evidence
