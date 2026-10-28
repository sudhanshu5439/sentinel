# Sentinel

[![ci](https://github.com/sudhanshu5439/sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/sudhanshu5439/sentinel/actions/workflows/ci.yml)
[![gitops validate](https://github.com/sudhanshu5439/sentinel-gitops/actions/workflows/validate.yml/badge.svg)](https://github.com/sudhanshu5439/sentinel-gitops/actions/workflows/validate.yml)

**A self-healing, supply-chain-secured Kubernetes delivery platform with an LLM-powered incident
assistant.** Built from scratch as a personal lab, in public.

- **Only trusted code runs.** Every image is scanned, signed (Sigstore keyless) and attested in CI;
  Kyverno refuses any image that is unsigned, not pinned by digest, or from a foreign registry.
- **Bad releases roll themselves back.** Argo Rollouts canaries are judged by Prometheus against
  error-rate and latency thresholds; a failing canary aborts and the stable version keeps serving.
- **Alerts mean user pain.** SLOs with multi-window, multi-burn-rate alerts (unit-tested).
- **An AI assistant investigates incidents** *(in progress)*: groups alerts, gathers evidence,
  correlates recent changes and asks an LLM for a diagnosis that must cite its evidence.

> Personal lab project on a laptop (kind) with an EKS demo planned. Not run in production; no
> client code or configuration. Any performance numbers will come only from measured runs in
> `chaos/results/`.

![Argo CD: every platform component managed from Git](docs/screenshots/2026-10-28-ingress/02-argocd-applications.png)

## Architecture

```mermaid
flowchart LR
  dev[git push] --> ci

  subgraph build[Build: GitHub Actions]
    ci[tests · Semgrep · Gitleaks] --> img[build · Trivy · Syft SBOM]
    img --> sign[cosign keyless sign + SLSA provenance] --> ghcr[(GHCR)]
  end

  sign -- "image digests" --> gitops[(sentinel-gitops repo)]

  subgraph cluster[Kubernetes: kind locally, EKS for the demo]
    argocd[Argo CD] --> kyverno{Kyverno: signed? by digest? limits set?}
    kyverno -- admitted --> rollouts[Argo Rollouts canary<br/>order-api · risk-engine · notifier]
    rollouts --> prom[Prometheus · Loki · Tempo<br/>Grafana · SLO alerts]
    prom -- analysis --> rollouts
    prom -- burn-rate alert --> am[Alertmanager]
    am -- webhook --> assistant[AIOps assistant]
  end

  gitops --> argocd
  ghcr --> rollouts
  assistant -- "evidence: metrics, logs, events, syncs" --> prom
  assistant -. "diagnosis (cited JSON)" .-> llm[LLM]
  assistant -. "Step 8: revert PR" .-> gitops
```

Two repositories, on purpose: this one holds the code, CI and tooling;
[**sentinel-gitops**](https://github.com/sudhanshu5439/sentinel-gitops) holds the desired state of
the cluster that Argo CD syncs. Releases, rollbacks and platform changes are all Git commits.

## Status

| # | Component | State |
|---|---|---|
| 1 | Demo apps: `order-api` (FastAPI), `risk-engine` (Go), `notifier` (Python), PostgreSQL, Redis | ✅ |
| 2 | Containers + local cluster: multi-stage non-root images, kind via Terraform, `make up` | ✅ |
| 3 | Secure CI: Gitleaks, Semgrep, Trivy, Syft SBOM, cosign keyless, SLSA provenance, GHCR | ✅ |
| 4 | GitOps + policy: Argo CD app-of-apps, promotion by digest via PR, Kyverno admission policies | ✅ |
| 5 | Progressive delivery: Argo Rollouts canary with Prometheus analysis, automatic abort | ✅ |
| 6 | Observability + SLOs: Alloy, Prometheus, Loki, Tempo, Grafana, burn-rate alerts, Traefik ingress | ✅ |
| 7 | AIOps assistant (read-only): ✅ alert grouping · ✅ evidence collection · ⏳ evidence pack, change correlation, LLM diagnosis, Slack | 🚧 |
| 8 | Guarded actions: action tiers, approvals, revert PRs, audit log, kill switch | planned |
| 9 | Chaos evaluation: 8 scenarios × 5 runs, assistant on vs. off | planned |
| 10 | EKS demo (apply, record, destroy), decision notes, video | planned |

Detailed log: [docs/PROGRESS.md](docs/PROGRESS.md) · Design: [docs/PLAN.md](docs/PLAN.md)

## How a release reaches production

1. Merge to `main` → CI runs secrets scan, SAST, lint and tests.
2. Images are built and **scanned before publishing** (fixable criticals block), pushed to GHCR,
   then signed with cosign keyless (GitHub OIDC, no stored keys) with SBOM and provenance attested.
3. CI commits the new **digests** to the staging overlay in sentinel-gitops and opens a PR for prod.
4. Argo CD syncs. Kyverno admits a pod only if its image is signed by this repo's
   `supply-chain.yml` on `main` (checked against the Rekor transparency log).
5. order-api rolls out as a canary (25 → 50 → 75 → 100 %) while Prometheus checks the canary pods
   only: enough traffic, ≤ 5 % errors, p95 ≤ 300 ms. Two failed checks abort the rollout.

![Argo Rollouts: a completed canary in prod](docs/screenshots/2026-10-28-ingress/06-rollouts-prod.png)

## The incident assistant (Step 7, in progress)

```
Alertmanager ──webhook──▶ sentinel-assistant (FastAPI)
                          1. group      alerts → 1 incident (namespace + service, 15-min window)  ✅
                          2. collect    Prometheus · Loki · K8s events · Argo CD / Rollouts       ✅
                          3. trim       evidence pack, each item gets an ID (E1, E2…), ~6k tokens
                          4. correlate  recent syncs / rollouts / config changes, scored
                          5. diagnose   LLM → schema-validated JSON; every claim cites an E-id;
                                        invalid → one retry → "unknown, escalate"
                          6. notify     Slack + GET /incidents
```

Design rules: read-only until Step 8; one alert storm = one investigation = one LLM call; a
data source that fails becomes an explicit *gap* in the evidence, never a silent omission; the
LLM call has a timeout, a token cap and a per-incident budget.

## Things that broke, and what they taught me

Real problems found while building this, each fixed and written up in
[docs/PROGRESS.md](docs/PROGRESS.md):

- **Metrics that could never catch a bad release.** The fault-injection middleware was outermost,
  so injected 500s never reached Prometheus: a canary analysis would have passed a broken
  version. Fixed by making middleware order explicit and testing it.
- **Canary pods received no traffic.** The load generator reused one keep-alive connection, and
  a Service balances per *connection*, so every request hit the same pod and the analysis failed
  for lack of data. Fixed with one connection per request.
- **A laptop "outage" diagnosed with PSI.** API-server restarts, slow etcd reads and probe
  timeouts traced to memory pressure (`/proc/pressure/memory` full avg60 ≈ 20) because WSL had
  7.6 GiB instead of the planned 10 GB.
- **An Argo CD sync-wave deadlock.** The root app waited for the ingress controller to become
  healthy before applying the very commit that would make it healthy.
- **A Helm value silently ignored.** `service.type` vs the chart's `service.spec.type`: the
  Service stayed `LoadBalancer <pending>` in kind. Lesson: assert on the rendered object, not on
  the values you think you set.

## Run it locally

Needs Windows 11 + WSL2 (or Linux), Docker, ~10 GB RAM for the full stack. Setup guide:
[SETUP.md](SETUP.md).

```bash
make up            # kind cluster (Terraform) + images + apps
make smoke         # place orders end to end
make gitops-up     # install Argo CD and hand the cluster to sentinel-gitops
make urls          # Grafana, Argo CD, Prometheus, Alertmanager, Rollouts via http://<tool>.localtest.me:8080
make canary-bad ENV=staging RATE=0.3   # release a broken version through Git, watch it abort
make test lint     # all unit tests and linters
make help          # everything else
```

## Repository layout

```
apps/{order-api,risk-engine,notifier}/   services, tests, Dockerfiles
aiops/                                   the incident assistant (FastAPI, Python 3.12, uv)
.github/workflows/                       ci.yml, supply-chain.yml (reusable), promotion
infra/terraform/                         kind cluster (EKS modules planned)
deploy/                                  pre-GitOps manifests and Argo CD bootstrap patches
docs/                                    PLAN, PROGRESS, runbooks, decisions, screenshots
```

## Stack

Kubernetes (kind) · Terraform · GitHub Actions · Semgrep · Gitleaks · Trivy · Syft · Sigstore
cosign · SLSA provenance · GHCR · Argo CD · Kyverno · Argo Rollouts · Traefik · Prometheus ·
Alertmanager · Grafana · Loki · Tempo · Grafana Alloy (OpenTelemetry) · Python (FastAPI, uv,
ruff, pytest) · Go (standard library)

---

Built by [Sudhanshu Mishra](https://github.com/sudhanshu5439): platform / DevOps engineer, CKA,
AWS Certified Security and Advanced Networking Specialty, HashiCorp Terraform Associate.
