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
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from .config import Settings
from .evidence import Evidence, EvidenceKind, Scope

log = logging.getLogger("assistant.collectors")

# Every app in the namespace, not only the alerting one: a DB outage or a slow dependency shows up
# in another app's numbers, and that is how the diagnosis avoids blaming the latest deploy.
RED_QUERIES = {
    "5xx ratio (5m)": (
        'sum by (app) (rate(http_requests_total{{namespace="{ns}",status="5xx"}}[5m]))'
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
    "pod not ready": 'kube_pod_status_ready{{namespace="{ns}",condition="false"}} == 1',
}
BASELINE_OFFSET = timedelta(hours=1)
LOG_LIMIT = 200


class Collector(Protocol):
    name: str

    async def collect(self, scope: Scope) -> list[Evidence]: ...


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


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
        for name, template in RED_QUERIES.items():
            expr = template.format(ns=ns)
            now, before = await asyncio.gather(
                self._query(expr, scope.end), self._query(expr, scope.end - BASELINE_OFFSET)
            )
            baseline = {s["metric"].get("app"): s["value"][1] for s in before}
            for series in now:
                app, value = series["metric"].get("app", "?"), series["value"][1]
                if value == "NaN":  # 0/0: no traffic at all, reported by requests per second
                    continue
                items.append(
                    Evidence(
                        kind=EvidenceKind.METRIC,
                        source=self.name,
                        title=f"{app} {name}",
                        detail=f"now {float(value):.4g}, 1h earlier "
                        + (
                            f"{float(baseline[app]):.4g}"
                            if baseline.get(app, "NaN") != "NaN"
                            else "n/a"
                        ),
                        at=scope.end,
                        data={
                            "app": app,
                            "now": value,
                            "baseline": baseline.get(app),
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
                "query": f'{{namespace="{scope.namespace}", level=~"error|warn|warning|critical"}}',
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
                        title=f"{labels.get('app', '?')} {labels.get('level', '?')} log",
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

    async def _get(self, path: str, **params: str) -> dict[str, Any]:
        r = await self._http.get(f"{self._url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def collect(self, scope: Scope) -> list[Evidence]:
        events, apps, rollouts = await asyncio.gather(
            self._get(f"/api/v1/namespaces/{scope.namespace}/events", fieldSelector="type=Warning"),
            self._get(f"/apis/argoproj.io/v1alpha1/namespaces/{self._argocd_ns}/applications"),
            self._get(f"/apis/argoproj.io/v1alpha1/namespaces/{scope.namespace}/rollouts"),
        )
        return (
            self._events(events, scope) + self._syncs(apps, scope) + self._rollouts(rollouts, scope)
        )

    def _events(self, body: dict[str, Any], scope: Scope) -> list[Evidence]:
        items = []
        for e in body.get("items", []):
            at = _ts(
                e.get("lastTimestamp") or e.get("eventTime") or e["metadata"]["creationTimestamp"]
            )
            if at is None or at < scope.start:
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
                        data={"app": name, "revision": h.get("revision")},
                    )
                )
        return items

    def _rollouts(self, body: dict[str, Any], scope: Scope) -> list[Evidence]:
        items = []
        for ro in body.get("items", []):
            name, status = ro["metadata"]["name"], ro.get("status", {})
            for c in status.get("conditions", []):
                at = _ts(c.get("lastUpdateTime"))
                if at is None or at < scope.start:
                    continue
                items.append(
                    Evidence(
                        kind=EvidenceKind.CHANGE,
                        source="rollouts",
                        title=f"rollout {name}: {c['type']}={c['status']} ({c.get('reason', '')})",
                        detail=c.get("message", ""),
                        at=at,
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
