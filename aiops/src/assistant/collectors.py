"""Collectors: gather raw evidence for an incident from Prometheus, Loki and the Kubernetes API.

Each collector is read-only and independent. collect_all() runs them concurrently with a timeout;
a collector that fails or times out becomes a GAP item instead of failing the incident, because
the diagnosis must know what it could not see ("no logs" is not the same as "logs unavailable").

Argo CD and Argo Rollouts are read as Kubernetes custom resources (Application, Rollout), so the
assistant needs only a read-only ServiceAccount, not an Argo CD login.
"""

import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from .config import Settings
from .evidence import Evidence, EvidenceKind, Scope

log = logging.getLogger("assistant.collectors")

# Every app in the namespace, not only the alerting one: a DB outage or a slow dependency shows up
# in another app's numbers, and that is how the diagnosis avoids blaming the latest deploy.
RED_QUERIES = {
    # `or 0 * total`: with no 5xx series at all, a healthy app must read 0, not vanish (a missing
    # number looks like "unknown" to the diagnosis).
    "5xx ratio (5m)": (
        '(sum by (app) (rate(http_requests_total{{namespace="{ns}",status="5xx"}}[5m]))'
        ' or 0 * sum by (app) (rate(http_requests_total{{namespace="{ns}"}}[5m])))'
        ' / sum by (app) (rate(http_requests_total{{namespace="{ns}"}}[5m]))'
    ),
    "p95 latency seconds (5m)": (
        "histogram_quantile(0.95, sum by (app, le) "
        '(rate(http_request_duration_seconds_bucket{{namespace="{ns}"}}[5m])))'
    ),
    "requests per second (5m)": 'sum by (app) (rate(http_requests_total{{namespace="{ns}"}}[5m]))',
}
STATE_QUERIES = {
    "container restarts in window": (
        "sum by (pod, container) "
        '(increase(kube_pod_container_status_restarts_total{{namespace="{ns}"}}[{window}s])) > 0'
    ),
    # Only terminations inside the window: the reason metric keeps old terminations forever.
    "terminated": (
        'kube_pod_container_status_last_terminated_reason{{namespace="{ns}"}} == 1'
        " and on (pod, container) "
        '(kube_pod_container_status_last_terminated_timestamp{{namespace="{ns}"}} > {start})'
    ),
    # `sum by (pod)`: the raw series carries kube-state-metrics' *own* container label.
    "pod not ready": (
        'sum by (pod) (kube_pod_status_ready{{namespace="{ns}",condition="false"}}) == 1'
    ),
}
BASELINE_OFFSET = timedelta(hours=1)
LOG_LIMIT = 200
# Matched on the line, not on a `level` label: tracebacks and driver exceptions are plain text
# without a level, and in the 7f live run the one line naming the cause (`relation "orders"
# does not exist`) was exactly such a line. Repeats are folded into one counted item by the pack.
LOG_PATTERN = (
    r"(?i)(error|exception|traceback|fatal|critical|panic|warn|refused|timeout|does not exist)"
)
# Python tracebacks reach Loki one line at a time. Frames (`  File ".../errors.py"`), source
# excerpts and `^^^^` markers are indented and match LOG_PATTERN only by accident (a path named
# errors.py); in the 7f run they were half the log items. The header adds nothing the exception
# line does not. Excluded in the query, so they also stop using up LOG_LIMIT. What remains is
# the unindented final line, `psycopg.errors.UndefinedTable: relation "orders" does not exist`.
TRACEBACK_NOISE = (
    r"^\s|^Traceback \(most recent call last\)"
    r"|^During handling of the above exception|^The above exception was the direct cause"
)
METADATA_ONLY = {"Accept": "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"}
# Argo Rollouts: "Rollout not completed, started update to revision 16 (58cb94cdf4)"
_ROLLOUT_UPDATE = re.compile(r"started update to revision (\d+) \(([0-9a-z]+)\)")


def _template_refs(rs: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(images, ConfigMap names) referenced by a ReplicaSet's pod template."""
    spec = rs["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    images = [_short_image(c.get("image", "?")) for c in containers]
    configs = {v["configMap"]["name"] for v in spec.get("volumes", []) if "configMap" in v}
    for c in containers:
        configs |= {e["configMapRef"]["name"] for e in c.get("envFrom", []) if "configMapRef" in e}
        configs |= {
            e["valueFrom"]["configMapKeyRef"]["name"]
            for e in c.get("env", [])
            if "configMapKeyRef" in e.get("valueFrom", {})
        }
    return images, sorted(configs)


def _short_image(image: str) -> str:
    """ghcr.io/x/sentinel-order-api@sha256:0c49...  ->  sentinel-order-api@sha256:0c494392"""
    name = image.rsplit("/", 1)[-1]
    if "@sha256:" in name:
        repo, digest = name.split("@sha256:", 1)
        return f"{repo}@sha256:{digest[:8]}"
    return name


class Collector(Protocol):
    name: str

    async def collect(self, scope: Scope) -> list[Evidence]: ...


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _value(result: list[dict[str, Any]], app: str) -> str | None:
    """One app's sample from an instant-query result; None if absent or NaN (no data)."""
    for series in result:
        if series["metric"].get("app", "?") == app and series["value"][1] != "NaN":
            return series["value"][1]
    return None


class PrometheusCollector:
    name = "prometheus"

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http, self._url = http, base_url.rstrip("/")

    async def _query(self, expr: str, at: datetime) -> list[dict[str, Any]]:
        r = await self._http.get(
            f"{self._url}/api/v1/query", params={"query": expr, "time": at.timestamp()}
        )
        r.raise_for_status()
        return r.json()["data"]["result"]

    async def collect(self, scope: Scope) -> list[Evidence]:
        ns, window = scope.namespace, int((scope.end - scope.start).total_seconds())
        items: list[Evidence] = []
        window_label = f"at window start ({window // 60} min earlier)"
        for name, template in RED_QUERIES.items():
            expr = template.format(ns=ns)
            now, hour_ago, at_start = await asyncio.gather(
                self._query(expr, scope.end),
                self._query(expr, scope.end - BASELINE_OFFSET),
                self._query(expr, scope.start),
            )
            for series in now:
                app, value = series["metric"].get("app", "?"), series["value"][1]
                if value == "NaN":  # 0/0: no traffic at all, reported by requests per second
                    continue
                detail = f"now {float(value):.4g}, 1h earlier "
                base, base_label = _value(hour_ago, app), "1h earlier"
                if base is None:
                    # No data an hour ago (in the 7f runs the laptop had been off): fall back to
                    # the start of the window, named as such, rather than give the model nothing.
                    base, base_label = _value(at_start, app), window_label
                    detail += "n/a (no data)"
                    if base is not None:
                        detail += f", {window_label} {float(base):.4g}"
                else:
                    detail += f"{float(base):.4g}"
                items.append(
                    Evidence(
                        kind=EvidenceKind.METRIC,
                        source=self.name,
                        title=f"{app} {name}",
                        detail=detail,
                        at=scope.end,
                        data={
                            "app": app,
                            "now": value,
                            "baseline": base,
                            "baseline_at": base_label if base is not None else None,
                            "query": expr,
                        },
                    )
                )
        for name, template in STATE_QUERIES.items():
            expr = template.format(ns=ns, window=window, start=int(scope.start.timestamp()))
            for series in await self._query(expr, scope.end):
                m = series["metric"]
                what = (
                    f"{m.get('pod', '?')}/{m['container']}"
                    if "container" in m
                    else m.get("pod", "?")
                )
                reason = f" ({m['reason']})" if "reason" in m else ""
                items.append(
                    Evidence(
                        kind=EvidenceKind.METRIC,
                        source=self.name,
                        title=f"{what} {name}{reason}",
                        detail=f"value {series['value'][1]}",
                        at=scope.end,
                        data={"labels": m, "query": expr},
                    )
                )
        return items


class LokiCollector:
    name = "loki"

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http, self._url = http, base_url.rstrip("/")

    async def collect(self, scope: Scope) -> list[Evidence]:
        r = await self._http.get(
            f"{self._url}/loki/api/v1/query_range",
            params={
                "query": (
                    f'{{namespace="{scope.namespace}"}} !~ `{TRACEBACK_NOISE}` |~ `{LOG_PATTERN}`'
                ),
                "start": int(scope.start.timestamp() * 1e9),
                "end": int(scope.end.timestamp() * 1e9),
                "limit": LOG_LIMIT,
                "direction": "backward",
            },
        )
        r.raise_for_status()
        items = []
        for stream in r.json()["data"]["result"]:
            labels = stream["stream"]
            for ts_ns, line in stream["values"]:
                items.append(
                    Evidence(
                        kind=EvidenceKind.LOG,
                        source=self.name,
                        title=f"{labels.get('app', '?')} {labels.get('level') or 'unlabelled'} log",
                        detail=line,
                        at=datetime.fromtimestamp(int(ts_ns) / 1e9, scope.end.tzinfo),
                        data={"app": labels.get("app"), "pod": labels.get("pod")},
                    )
                )
        return items


class KubernetesCollector:
    """Warning events, Argo CD sync history and Argo Rollouts status, via the Kubernetes API."""

    name = "kubernetes"

    def __init__(self, http: httpx.AsyncClient, api_url: str, argocd_namespace: str) -> None:
        self._http, self._url, self._argocd_ns = http, api_url.rstrip("/"), argocd_namespace

    async def _get(
        self, path: str, headers: dict[str, str] | None = None, **params: str
    ) -> dict[str, Any]:
        r = await self._http.get(f"{self._url}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    async def collect(self, scope: Scope) -> list[Evidence]:
        ns = scope.namespace
        events, apps, rollouts, replicasets, configmaps, analysisruns = await asyncio.gather(
            # All events: Warnings are evidence; Normal "started update to revision N (hash)"
            # events reveal re-deployed revisions (see _revisions).
            self._get(f"/api/v1/namespaces/{ns}/events"),
            self._get(f"/apis/argoproj.io/v1alpha1/namespaces/{self._argocd_ns}/applications"),
            self._get(f"/apis/argoproj.io/v1alpha1/namespaces/{ns}/rollouts"),
            self._get(f"/apis/apps/v1/namespaces/{ns}/replicasets"),
            # Metadata only: the assistant needs to know a ConfigMap appeared, never its contents.
            self._get(f"/api/v1/namespaces/{ns}/configmaps", headers=METADATA_ONLY),
            self._get(f"/apis/argoproj.io/v1alpha1/namespaces/{ns}/analysisruns"),
        )
        return (
            self._events(events, scope)
            + self._syncs(apps, scope)
            + self._rollouts(rollouts, scope)
            + self._revisions(replicasets, events, rollouts, analysisruns, scope)
            + self._configs(configmaps, scope)
        )

    def _revisions(
        self,
        replicasets: dict[str, Any],
        events: dict[str, Any],
        rollouts: dict[str, Any],
        analysisruns: dict[str, Any],
        scope: Scope,
    ) -> list[Evidence]:
        """The workload really changed (unlike an Argo CD sync, which may have changed nothing)
        when a revision was deployed in the window. Two ways that shows up:

        - a ReplicaSet *created* in the window (a pod template never seen before);
        - a rollout *re-deploying* an existing ReplicaSet: Argo Rollouts re-uses the ReplicaSet
          of an identical template, so a rollback or a repeated release creates no new object.
          Its AnalysisRun (and, best-effort, a "started update to revision N" event) dates it.

        Each revision is diffed against its predecessor (the previous ReplicaSet, or the
        rollout's stable one), so the diagnosis sees *what* changed: the image (code) or only the
        referenced ConfigMaps (config). Config needs this: kustomize names ConfigMaps by content
        hash and old versions are kept, so re-applying a known config creates no new ConfigMap."""
        rs_by_name = {rs["metadata"]["name"]: rs for rs in replicasets.get("items", [])}
        by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rs in rs_by_name.values():
            owner = (rs["metadata"].get("ownerReferences") or [{}])[0]
            by_owner[
                f"{owner.get('kind', '?')}/{owner.get('name', rs['metadata']['name'])}"
            ].append(rs)

        stable = {
            r["metadata"]["name"]: r.get("status", {}).get("stableRS")
            for r in rollouts.get("items", [])
        }

        def predecessor(owner_ref: str, rs: dict[str, Any], fallback: dict[str, Any] | None):
            """What this revision replaces: a rollout's stable ReplicaSet, else the previous one."""
            kind, name = owner_ref.split("/", 1)
            candidate = rs_by_name.get(f"{name}-{stable.get(name)}") if kind == "Rollout" else None
            candidate = candidate or fallback
            return None if candidate is rs else candidate

        deployed: list[tuple[str, dict[str, Any], dict[str, Any] | None, datetime, str]] = []
        for owner_ref, revisions in by_owner.items():
            revisions.sort(key=lambda rs: rs["metadata"].get("creationTimestamp", ""))
            for previous, rs in zip([None, *revisions], revisions, strict=False):
                at = _ts(rs["metadata"].get("creationTimestamp"))
                if at is not None and at >= scope.start:
                    prev = predecessor(owner_ref, rs, previous)
                    deployed.append((owner_ref, rs, prev, at, "new revision of"))

        # Every attempt to roll out a revision, keyed (rollout, revision number), earliest time.
        # AnalysisRuns are the durable record (one per attempt, named <rs>-<revision>); events add
        # rollouts without analysis but are best-effort: the event recorder rate-limits per
        # object, and during rapid releases it silently dropped "revision 20" entirely.
        attempts: dict[tuple[str, str], tuple[str, datetime]] = {}

        def attempt(rollout: str, revision: str, pod_hash: str, at: datetime | None) -> None:
            if at is None or at < scope.start:
                return
            key = (rollout, revision)
            if key not in attempts or at < attempts[key][1]:
                attempts[key] = (pod_hash, at)

        for run in analysisruns.get("items", []):
            meta = run["metadata"]
            owner = (meta.get("ownerReferences") or [{}])[0]
            pod_hash = meta.get("labels", {}).get("rollouts-pod-template-hash")
            revision = meta.get("annotations", {}).get("rollout.argoproj.io/revision")
            if owner.get("kind") == "Rollout" and pod_hash and revision:
                attempt(owner["name"], revision, pod_hash, _ts(meta.get("creationTimestamp")))
        for e in events.get("items", []):
            match = _ROLLOUT_UPDATE.search(e.get("message", ""))
            if e.get("reason") == "RolloutNotCompleted" and match:
                at = _ts(e.get("lastTimestamp") or e.get("eventTime"))
                attempt(e["involvedObject"]["name"], match.group(1), match.group(2), at)

        for (rollout, revision), (pod_hash, at) in sorted(
            attempts.items(), key=lambda kv: kv[1][1]
        ):
            rs = rs_by_name.get(f"{rollout}-{pod_hash}")
            created = _ts(rs["metadata"].get("creationTimestamp")) if rs else None
            # Every deployment counts (a revision can be released several times in the window);
            # skip only the attempt that *is* the creation, already reported as a new revision.
            if rs is None or (created is not None and abs((at - created).total_seconds()) < 60):
                continue
            how = f"re-deployed earlier revision (rollout revision {revision}) of"
            owner_ref = f"Rollout/{rollout}"
            deployed.append((owner_ref, rs, predecessor(owner_ref, rs, None), at, how))

        items = []
        for owner_ref, rs, previous, at, how in deployed:
            images, configs = _template_refs(rs)
            old_images, old_configs = _template_refs(previous) if previous else (None, None)
            what = []
            if old_images is None:
                what.append(f"image {', '.join(images)} (no predecessor to compare)")
            elif images != old_images:
                what.append(f"image {', '.join(old_images)} → {', '.join(images)}")
            else:
                what.append(f"image unchanged ({', '.join(images)})")
            if old_configs is not None and configs != old_configs:
                what.append(f"config {', '.join(old_configs) or '-'} → {', '.join(configs) or '-'}")
            items.append(
                Evidence(
                    kind=EvidenceKind.CHANGE,
                    source="kubernetes",
                    title=f"{how} {owner_ref}",
                    detail=f"ReplicaSet {rs['metadata']['name']}; {'; '.join(what)}",
                    at=at,
                    data={
                        "change": "workload",
                        "owner": owner_ref.split("/", 1)[1],
                        "image_changed": old_images is not None and images != old_images,
                        "config_changed": old_configs is not None and configs != old_configs,
                    },
                )
            )
        return items

    def _configs(self, body: dict[str, Any], scope: Scope) -> list[Evidence]:
        """Kustomize-generated ConfigMaps carry a content hash in their name, so a ConfigMap
        created in the window is a new configuration version."""
        items = []
        for cm in body.get("items", []):
            meta = cm["metadata"]
            at = _ts(meta.get("creationTimestamp"))
            if at is None or at < scope.start or meta["name"] == "kube-root-ca.crt":
                continue
            items.append(
                Evidence(
                    kind=EvidenceKind.CHANGE,
                    source="kubernetes",
                    title=f"new ConfigMap {meta['name']}",
                    detail="configuration version created (contents not read)",
                    at=at,
                    data={"change": "config", "name": meta["name"]},
                )
            )
        return items

    def _events(self, body: dict[str, Any], scope: Scope) -> list[Evidence]:
        items = []
        for e in body.get("items", []):
            at = _ts(
                e.get("lastTimestamp") or e.get("eventTime") or e["metadata"]["creationTimestamp"]
            )
            if at is None or at < scope.start or e.get("type") != "Warning":
                continue
            obj = e["involvedObject"]
            items.append(
                Evidence(
                    kind=EvidenceKind.EVENT,
                    source="kubernetes",
                    title=f"{e.get('reason', '?')} on {obj['kind']}/{obj['name']}",
                    detail=e.get("message", ""),
                    at=at,
                    data={"count": e.get("count", 1)},
                )
            )
        return items

    def _syncs(self, body: dict[str, Any], scope: Scope) -> list[Evidence]:
        items = []
        for app in body.get("items", []):
            if app["spec"].get("destination", {}).get("namespace") != scope.namespace:
                continue
            name = app["metadata"]["name"]
            for h in app.get("status", {}).get("history", []):
                at = _ts(h.get("deployedAt"))
                if at is None or at < scope.start:
                    continue
                # A history entry means "synced at this Git revision", not "this app changed":
                # any commit to the repo moves the revision. The correlator (7d) must check that.
                items.append(
                    Evidence(
                        kind=EvidenceKind.CHANGE,
                        source="argocd",
                        title=f"Argo CD synced {name} to {h.get('revision', '?')[:7]}",
                        detail=f"source path {h.get('source', {}).get('path', '?')}",
                        at=at,
                        data={"change": "sync", "app": name, "revision": h.get("revision")},
                    )
                )
        return items

    def _rollouts(self, body: dict[str, Any], scope: Scope) -> list[Evidence]:
        items = []
        for ro in body.get("items", []):
            name, status = ro["metadata"]["name"], ro.get("status", {})
            # One item per rollout: five condition flips of one release are one fact, not five.
            recent = [
                c
                for c in status.get("conditions", [])
                if (at := _ts(c.get("lastUpdateTime"))) is not None and at >= scope.start
            ]
            if recent:
                recent.sort(key=lambda c: c["lastUpdateTime"])
                flips = "; ".join(
                    f"{c['type']}={c['status']} ({c.get('reason', '')})" for c in recent
                )
                items.append(
                    Evidence(
                        kind=EvidenceKind.CHANGE,
                        source="rollouts",
                        title=f"rollout {name} conditions changed",
                        detail=flips,
                        at=_ts(recent[-1]["lastUpdateTime"]),
                        data={"change": "rollout", "rollout": name},
                    )
                )
            if status.get("phase") not in (None, "Healthy"):
                items.append(
                    Evidence(
                        kind=EvidenceKind.CHANGE,
                        source="rollouts",
                        title=f"rollout {name} is {status['phase']}",
                        detail=status.get("message") or "",
                        at=scope.end,
                        data={
                            "change": "rollout",
                            "rollout": name,
                            "canary_hash": status.get("currentPodHash"),
                            "stable_hash": status.get("stableRS"),
                        },
                    )
                )
        return items


class TokenFileAuth(httpx.Auth):
    """Bearer token re-read on every request: the kubelet rotates projected ServiceAccount tokens,
    so a token read once at start-up eventually expires."""

    def __init__(self, path: str) -> None:
        self._path = path

    def auth_flow(self, request: httpx.Request):
        with open(self._path) as f:
            request.headers["Authorization"] = f"Bearer {f.read().strip()}"
        yield request


def build_collectors(settings: Settings) -> tuple[list[Collector], list[httpx.AsyncClient]]:
    """Collectors for every configured source, plus the HTTP clients to close on shutdown."""
    collectors: list[Collector] = []
    clients: list[httpx.AsyncClient] = []
    timeout = httpx.Timeout(settings.collect_timeout_seconds)
    if settings.prometheus_url or settings.loki_url:
        http = httpx.AsyncClient(timeout=timeout)
        clients.append(http)
        if settings.prometheus_url:
            collectors.append(PrometheusCollector(http, settings.prometheus_url))
        if settings.loki_url:
            collectors.append(LokiCollector(http, settings.loki_url))
    if settings.kube_api_url:
        in_cluster = os.path.exists(settings.kube_token_file)
        kube = httpx.AsyncClient(
            timeout=timeout,
            auth=TokenFileAuth(settings.kube_token_file) if in_cluster else None,
            verify=settings.kube_ca_file if in_cluster else True,
        )
        clients.append(kube)
        collectors.append(
            KubernetesCollector(kube, settings.kube_api_url, settings.argocd_namespace)
        )
    return collectors, clients


async def collect_all(scope: Scope, collectors: list[Collector], timeout: float) -> list[Evidence]:
    async def run(c: Collector) -> list[Evidence]:
        try:
            return await asyncio.wait_for(c.collect(scope), timeout)
        except Exception as exc:  # noqa: BLE001 - any failure becomes a visible gap
            log.warning("collector %s failed: %r", c.name, exc)
            return [
                Evidence(
                    kind=EvidenceKind.GAP,
                    source=c.name,
                    title=f"{c.name} unavailable",
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                    at=scope.end,
                )
            ]

    results = await asyncio.gather(*(run(c) for c in collectors))
    return [item for items in results for item in items]
