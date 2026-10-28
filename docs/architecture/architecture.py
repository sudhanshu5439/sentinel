"""Sentinel architecture diagram, as code (https://diagrams.mingrammer.com).

Regenerate with `make diagram` (renders in a throwaway container; nothing to install).
Official icons come from the diagrams library; Kyverno, Argo Rollouts, Sigstore cosign and
OpenTelemetry logos are in icons/ (see icons/SOURCES.md).
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.custom import Custom
from diagrams.k8s.network import Ing
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import Users
from diagrams.onprem.compute import Server
from diagrams.onprem.container import Docker
from diagrams.onprem.database import Postgresql
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.logging import Loki
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.onprem.network import Traefik
from diagrams.onprem.security import Trivy
from diagrams.onprem.tracing import Tempo
from diagrams.onprem.vcs import Github
from diagrams.programming.framework import Fastapi
from diagrams.programming.language import Go, Python
from diagrams.saas.chat import Slack

GRAPH = {
    "fontsize": "22",
    "fontname": "Helvetica",
    "pad": "0.4",
    "nodesep": "0.55",
    "ranksep": "1.0",
    "splines": "spline",
    "labelloc": "t",
}
NODE = {"fontsize": "12", "fontname": "Helvetica"}
EDGE = {"fontsize": "11", "fontname": "Helvetica"}
# Edges that point "backwards" in the flow are drawn but do not influence the layout.
BACK = {"constraint": "false"}
PLANNED = {"style": "dashed", "color": "gray40"}

with Diagram(
    "Sentinel: secure delivery, self-healing releases, AI incident assistant",
    filename="sentinel-architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH,
    node_attr=NODE,
    edge_attr=EDGE,
):
    with Cluster("Build: GitHub Actions (OIDC, no stored keys)"):
        code = Github("sentinel\n(code + CI)")
        checks = GithubActions("tests · Semgrep\nGitleaks")
        scan = Trivy("Trivy scan\n+ Syft SBOM")
        sign = Custom("cosign keyless\n+ SLSA provenance", "./icons/cosign.png")
        ghcr = Docker("GHCR\n(signed images)")
        code >> checks >> scan >> sign >> ghcr

    gitops = Github("sentinel-gitops\n(desired state)")
    sign >> Edge(label="digests → staging\nPR → prod") >> gitops

    with Cluster("Kubernetes: kind on a laptop · EKS for the demo"):
        with Cluster("Delivery + policy"):
            argocd = Argocd("Argo CD\napp-of-apps")
            kyverno = Custom("Kyverno\nsigned? by digest?\nlimits + probes?", "./icons/kyverno.png")
            rollouts = Custom("Argo Rollouts\ncanary + analysis", "./icons/argo.png")
            argocd >> Edge(label="admission") >> kyverno >> rollouts

        with Cluster("Trading demo (staging · prod)"):
            order_api = Fastapi("order-api")
            risk = Go("risk-engine")
            notifier = Python("notifier")
            db = Postgresql("PostgreSQL")
            cache = Redis("Redis Streams")
            order_api >> risk
            order_api >> db
            order_api >> cache >> notifier

        with Cluster("Observability + SLOs"):
            alloy = Custom("Alloy\n(OpenTelemetry)", "./icons/opentelemetry.png")
            prom = Prometheus("Prometheus\nburn-rate SLOs")
            alerts = Prometheus("Alertmanager")
            loki = Loki("Loki")
            tempo = Tempo("Tempo")
            grafana = Grafana("Grafana")
            alloy >> [loki, tempo]
            prom >> alerts
            [prom, loki, tempo] >> grafana

        with Cluster("AIOps (read-only today)"):
            assistant = Fastapi("sentinel-assistant\ngroup · collect\ncorrelate · diagnose")

        ingress = Traefik("Traefik ingress")

    llm = Server("LLM API\n(cited JSON,\ntoken + cost caps)")
    slack = Slack("Slack")
    people = Users("engineers")

    gitops >> Edge(label="sync") >> argocd
    rollouts >> Edge(label="25→50→75→100%") >> order_api
    ghcr >> Edge(label="pull by digest", style="dotted", **BACK) >> order_api
    order_api >> Edge(label="logs + traces") >> alloy
    order_api >> Edge(label="metrics") >> prom
    rollouts >> Edge(label="canary analysis (PromQL)", color="darkorange", **BACK) >> prom
    alerts >> Edge(label="webhook", color="firebrick") >> assistant
    assistant >> Edge(label="evidence (read-only)", color="firebrick", **BACK) >> prom
    assistant >> Edge(label="evidence pack", color="firebrick") >> llm
    assistant >> Edge(label="diagnosis", **PLANNED) >> slack
    assistant >> Edge(label="revert PR (Step 8)", **PLANNED, **BACK) >> gitops
    people >> ingress >> grafana
