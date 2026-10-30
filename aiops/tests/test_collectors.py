"""Collectors against fake backends (httpx.MockTransport). Response shapes copied from the real
Prometheus, Loki and Kubernetes APIs on the laptop cluster."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from assistant.collectors import (
    KubernetesCollector,
    LokiCollector,
    PrometheusCollector,
    collect_all,
)
from assistant.evidence import EvidenceKind, Scope

END = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
SCOPE = Scope(
    namespace="sentinel-staging", service="order-api", start=END - timedelta(minutes=30), end=END
)


def run(coro):
    return asyncio.run(coro)


def http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def prom_vector(*series) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def test_prometheus_reports_every_app_against_its_baseline():
    def handler(request: httpx.Request) -> httpx.Response:
        q, t = request.url.params["query"], float(request.url.params["time"])
        baseline = t < END.timestamp()
        if 'status="5xx"' in q:
            return httpx.Response(
                200,
                json=prom_vector(
                    {"metric": {"app": "order-api"}, "value": [t, "0.01" if baseline else "0.3"]},
                    {"metric": {"app": "risk-engine"}, "value": [t, "NaN"]},  # no traffic: skipped
                ),
            )
        if "restarts_total" in q and not baseline:
            series = {
                "metric": {"pod": "order-api-abc", "container": "order-api"},
                "value": [t, "3"],
            }
            return httpx.Response(200, json=prom_vector(series))
        return httpx.Response(200, json=prom_vector())

    items = run(PrometheusCollector(http(handler), "http://prom").collect(SCOPE))
    titles = {i.title: i for i in items}
    ratio = titles["order-api 5xx ratio (5m)"]
    assert ratio.kind is EvidenceKind.METRIC
    assert ratio.detail == "now 0.3, 1h earlier 0.01"
    assert "risk-engine 5xx ratio (5m)" not in titles
    assert "order-api-abc/order-api container restarts in window" in titles


def test_prometheus_falls_back_to_the_window_start_when_an_hour_ago_has_no_data():
    hour_ago = (END - timedelta(hours=1)).timestamp()

    def handler(request: httpx.Request) -> httpx.Response:
        q, t = request.url.params["query"], float(request.url.params["time"])
        if 'status="5xx"' not in q or t == hour_ago:  # the cluster was off an hour ago
            return httpx.Response(200, json=prom_vector())
        value = "0.3" if t == END.timestamp() else "0.002"
        return httpx.Response(
            200, json=prom_vector({"metric": {"app": "order-api"}, "value": [t, value]})
        )

    (ratio,) = run(PrometheusCollector(http(handler), "http://prom").collect(SCOPE))
    assert (
        ratio.detail == "now 0.3, 1h earlier n/a (no data), at window start (30 min earlier) 0.002"
    )
    assert ratio.data["baseline"] == "0.002"
    assert ratio.data["baseline_at"] == "at window start (30 min earlier)"


def test_prometheus_queries_only_the_incident_namespace():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return httpx.Response(200, json=prom_vector())

    run(PrometheusCollector(http(handler), "http://prom").collect(SCOPE))
    assert seen and all('namespace="sentinel-staging"' in q for q in seen)


def test_loki_returns_error_lines_with_and_without_a_level_label():
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        # A line filter, not a level selector: unlabelled tracebacks must be included.
        assert "level=" not in query and "|~" in query and "does not exist" in query
        # Indented traceback frames are excluded in the query, before LOG_LIMIT applies.
        assert "!~ `^\\s|^Traceback" in query
        ts = str(int(END.timestamp() * 1e9))
        return httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "stream": {"app": "order-api", "level": "error", "pod": "p"},
                            "values": [[ts, '{"level": "error", "msg": "risk-engine down"}']],
                        },
                        {
                            "stream": {"app": "order-api", "pod": "p"},  # no level label
                            "values": [
                                [ts, 'UndefinedTableError: relation "orders" does not exist']
                            ],
                        },
                    ]
                }
            },
        )

    labelled, unlabelled = run(LokiCollector(http(handler), "http://loki").collect(SCOPE))
    assert labelled.kind is EvidenceKind.LOG and labelled.title == "order-api error log"
    assert "risk-engine down" in labelled.detail and labelled.at == END
    assert unlabelled.title == "order-api unlabelled log"
    assert "does not exist" in unlabelled.detail


def kube_handler(request: httpx.Request) -> httpx.Response:
    recent, old = "2026-10-28T11:50:00Z", "2026-10-28T09:00:00Z"
    path = request.url.path
    if path.endswith("/events"):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "type": "Warning",
                        "reason": "BackOff",
                        "message": "Back-off restarting failed container",
                        "lastTimestamp": recent,
                        "count": 4,
                        "involvedObject": {"kind": "Pod", "name": "order-api-abc"},
                        "metadata": {"creationTimestamp": recent},
                    },
                    {
                        "type": "Warning",
                        "reason": "Unhealthy",
                        "lastTimestamp": old,  # before the window: dropped
                        "involvedObject": {"kind": "Pod", "name": "old"},
                        "metadata": {"creationTimestamp": old},
                    },
                    {
                        "type": "Normal",  # not a warning: not reported as an event...
                        "reason": "RolloutNotCompleted",
                        # ...but it reveals a re-deployed, already existing ReplicaSet
                        "message": "Rollout not completed, started update to revision 7 (reused)",
                        "lastTimestamp": recent,
                        "involvedObject": {"kind": "Rollout", "name": "order-api"},
                        "metadata": {"creationTimestamp": recent},
                    },
                ]
            },
        )
    if path.endswith("/applications"):
        app = {
            "metadata": {"name": "sentinel-staging"},
            "spec": {"destination": {"namespace": "sentinel-staging"}},
            "status": {
                "history": [
                    {"revision": "aaaaaaa111", "deployedAt": old},
                    {"revision": "bbbbbbb222", "deployedAt": recent, "source": {"path": "apps/x"}},
                ]
            },
        }
        other = {
            "metadata": {"name": "tempo"},
            "spec": {"destination": {"namespace": "monitoring"}},
        }
        return httpx.Response(200, json={"items": [app, other]})
    if path.endswith("/rollouts"):
        rollout = {
            "metadata": {"name": "order-api"},
            "status": {
                "phase": "Degraded",
                "message": "RolloutAborted: canary-traffic failed",
                "currentPodHash": "new",
                "stableRS": "old",
                "conditions": [
                    {
                        "type": "Progressing",
                        "status": "False",
                        "reason": "RolloutAborted",
                        "lastUpdateTime": recent,
                        "message": "aborted",
                    },
                    {"type": "Available", "status": "True", "lastUpdateTime": old},
                ],
            },
        }
        return httpx.Response(200, json={"items": [rollout]})
    if path.endswith("/replicasets"):
        image = "ghcr.io/sudhanshu5439/sentinel-order-api@sha256:0c49439247f1d69c33d2"

        def rs(name, created, faults_config, replicas):
            container = {"image": image, "envFrom": [{"configMapRef": {"name": faults_config}}]}
            return {
                "metadata": {
                    "name": name,
                    "creationTimestamp": created,
                    "ownerReferences": [{"kind": "Rollout", "name": "order-api"}],
                },
                "spec": {"template": {"spec": {"containers": [container]}}},
                "status": {"replicas": replicas},
            }

        # Same image, different (re-used, already existing) config: a config-only release.
        # The rollout is Degraded (aborted): the new canary was scaled to 0, stable still runs.
        items = [
            rs("order-api-new", recent, "order-api-faults-5m8b668dd4", 0),
            rs("order-api-old", old, "order-api-faults-ok", 4),  # the rollout's stableRS ("old")
            rs("order-api-reused", old, "order-api-faults-slow", 0),  # long ago, re-deployed
        ]
        return httpx.Response(200, json={"items": items})
    if path.endswith("/analysisruns"):

        def run_of(rs_hash, revision, created):
            return {
                "metadata": {
                    "name": f"order-api-{rs_hash}-{revision}",
                    "creationTimestamp": created,
                    "labels": {"rollouts-pod-template-hash": rs_hash},
                    "annotations": {"rollout.argoproj.io/revision": revision},
                    "ownerReferences": [{"kind": "Rollout", "name": "order-api"}],
                }
            }

        items = [
            run_of("new", "8", "2026-10-28T11:50:15Z"),  # same attempt as the RS creation
            run_of("reused", "9", "2026-10-28T11:55:00Z"),  # its event was dropped
        ]
        return httpx.Response(200, json={"items": items})
    if path.endswith("/configmaps"):
        assert "PartialObjectMetadataList" in request.headers["accept"]  # never read contents
        items = [
            {"metadata": {"name": "order-api-faults-5m8b668dd4", "creationTimestamp": recent}},
            {"metadata": {"name": "kube-root-ca.crt", "creationTimestamp": recent}},
            {"metadata": {"name": "order-api-faults-old", "creationTimestamp": old}},
        ]
        return httpx.Response(200, json={"items": items})
    return httpx.Response(404)


def test_kubernetes_events_syncs_and_rollouts_inside_the_window():
    items = run(KubernetesCollector(http(kube_handler), "http://kube", "argocd").collect(SCOPE))
    titles = [i.title for i in items]
    assert "BackOff on Pod/order-api-abc" in titles
    assert "Argo CD synced sentinel-staging to bbbbbbb" in titles
    assert not any("aaaaaaa" in t or "tempo" in t or "old" in t for t in titles)
    conditions = next(i for i in items if i.title == "rollout order-api conditions changed")
    assert conditions.detail == "Progressing=False (RolloutAborted)"  # old condition left out
    degraded = next(i for i in items if i.title == "rollout order-api is Degraded")
    assert degraded.data["canary_hash"] == "new" and degraded.data["stable_hash"] == "old"


def test_a_new_revision_is_diffed_against_the_previous_one():
    items = run(KubernetesCollector(http(kube_handler), "http://kube", "argocd").collect(SCOPE))
    by_title = {i.title: i for i in items}
    revision = by_title["new revision of Rollout/order-api"]
    assert revision.data == {
        "change": "workload",
        "owner": "order-api",
        "image_changed": False,
        "config_changed": True,
        "live": False,
    }
    assert "image unchanged (sentinel-order-api@sha256:0c494392)" in revision.detail
    assert "config order-api-faults-ok → order-api-faults-5m8b668dd4" in revision.detail
    config = by_title["new ConfigMap order-api-faults-5m8b668dd4"]
    assert config.data["change"] == "config"
    assert not any("kube-root-ca" in t or "faults-old" in t for t in by_title)


def test_changes_that_are_no_longer_running_say_so():
    """8b live run: a DB outage 13 min after an aborted canary was blamed on that canary's
    config, though its ReplicaSet had 0 replicas and the stable revision never changed."""
    items = run(KubernetesCollector(http(kube_handler), "http://kube", "argocd").collect(SCOPE))
    by_title = {i.title: i for i in items}
    revision = by_title["new revision of Rollout/order-api"]
    assert revision.data["live"] is False
    assert revision.detail.endswith("no longer running (0 replicas)")
    config = by_title["new ConfigMap order-api-faults-5m8b668dd4"]
    assert config.data["live"] is False and "no running pod uses it" in config.detail


def test_a_re_deployed_existing_revision_is_reported_from_its_rollout_event():
    items = run(KubernetesCollector(http(kube_handler), "http://kube", "argocd").collect(SCOPE))
    redeploy = next(i for i in items if i.title.startswith("re-deployed earlier revision"))
    assert (
        redeploy.title == "re-deployed earlier revision (rollout revision 7) of Rollout/order-api"
    )
    assert "ReplicaSet order-api-reused" in redeploy.detail
    assert "config order-api-faults-ok → order-api-faults-slow" in redeploy.detail  # vs stable
    assert redeploy.data["change"] == "workload" and redeploy.data["config_changed"] is True
    # order-api-new: its creation and its AnalysisRun 15 s later are one attempt, reported once
    assert sum(i.title == "new revision of Rollout/order-api" for i in items) == 1
    # a re-deploy known only from its AnalysisRun (the event recorder dropped the event)
    assert any(
        i.title.startswith("re-deployed earlier revision (rollout revision 9)") for i in items
    )
    # the Normal event itself is not reported as a warning event
    assert not any(i.kind is EvidenceKind.EVENT and "RolloutNotCompleted" in i.title for i in items)


class Broken:
    name = "loki"

    async def collect(self, scope):
        raise httpx.ConnectError("connection refused")


class Slow:
    name = "prometheus"

    async def collect(self, scope):
        await asyncio.sleep(5)
        return []


def test_failing_or_slow_collectors_become_gaps_not_failures():
    kube = KubernetesCollector(http(kube_handler), "http://kube", "argocd")
    items = run(collect_all(SCOPE, [Broken(), Slow(), kube], timeout=0.2))
    gaps = {i.source: i for i in items if i.kind is EvidenceKind.GAP}
    assert set(gaps) == {"loki", "prometheus"}
    assert "ConnectError" in gaps["loki"].detail
    assert "TimeoutError" in gaps["prometheus"].detail
    assert any(i.source == "kubernetes" for i in items)  # the healthy collector still delivered


@pytest.mark.parametrize("bad", ['staging"} or vector(1) #', "UPPER", "a/b", ""])
def test_scope_rejects_namespaces_that_could_inject_queries(bad):
    with pytest.raises(ValueError):
        Scope(namespace=bad, service="x", start=END, end=END)
