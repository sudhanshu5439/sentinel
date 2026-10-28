# Sentinel: Self-Healing Secure Delivery Platform (design)

## What it is

A Kubernetes platform where only signed, policy-checked code reaches production, a bad release
rolls itself back, and an AI incident assistant diagnoses failures and takes safe fixes, with
human approval for risky ones. It runs on a laptop (kind) with `make up`, and on AWS EKS for the
demo.

**Pitch:** Built a self-healing, supply-chain-secured Kubernetes delivery platform with an
LLM-powered incident assistant; measured across 8 chaos scenarios for detection time, recovery time
and diagnosis accuracy.

**Demo application:** a small trading-style system: `order-api` (Python FastAPI), `risk-engine`
(Go), `notifier` (Python), plus PostgreSQL and Redis. Personal lab, built from scratch, no client
code or configuration.

## Architecture: five layers

1. **Build (GitHub):** code push → GitHub Actions CI (tests, Semgrep, Gitleaks, Trivy) → GHCR
   (signed image, SBOM, provenance).
2. **Deliver (GitOps):** CI opens a promotion PR with the image digest in `sentinel-gitops` →
   Argo CD syncs Git to the cluster → Kyverno admits only images signed by this repo's workflow
   and compliant with policies.
3. **Run (Kubernetes):** Argo Rollouts (canary + analysis), the three services, data stores.
   Chaos Mesh injects faults here.
4. **Observe:** OpenTelemetry Collector → Prometheus (metrics), Loki (logs), Tempo (traces) →
   Grafana dashboards + Sloth SLOs with multi-window burn-rate alerts → Alertmanager.
5. **Respond (AIOps):** Alertmanager webhook → incident assistant (group, collect, correlate,
   diagnose, act within tiers, audit) ↔ LLM (hosted API or Ollama; cited JSON output) ↔ Slack
   approvals. **Fixes go back through Git** (revert PR) or the Rollouts API (abort canary).

## Flow 1: commit to production

1. Merge to main (PR reviewed)
2. Code checks: unit tests, Semgrep SAST, Gitleaks → fail = stop and notify the author
3. Build image: Trivy scan, Syft SBOM → critical CVEs = stop and notify
4. Sign and attest: Cosign keyless, SLSA provenance
5. Promote by pull request: digest to staging, then prod
6. Sync and admit: Argo CD sync, Kyverno verifies the signature → unsigned = rejected at admission
7. Canary at 20% with k6 traffic and Prometheus analysis
8. SLOs healthy? → yes: promote to 100% · no: automatic rollback + incident opened (starts Flow 2)

## Flow 2: incident to resolution

1. Burn-rate alert fires → Alertmanager webhook
2. Group into one incident (same service and time window)
3. Collect and trim evidence: errors, top metrics, events, one trace
4. Correlate with recent changes: Argo CD syncs and config, scored
5. Diagnose with the LLM: evidence pack in, cited JSON out
6. Valid and cited? → no: escalate to on-call, no automatic action
7. Action tier:
   - Tier 0: notify (diagnosis to Slack)
   - Tier 1: act now (abort canary, revert PR), with blast-radius and rate limits
   - Tier 2: approve first (Slack button): scale out, config change
   - Tier 3: never (delete volumes or namespaces)
8. SLO back to normal? → no: escalate with the full evidence pack · yes: close
9. Close and learn: audit log + postmortem draft as a GitHub issue

## Components and choices

| Layer | Tool | Why (alternative) |
|---|---|---|
| Demo app | order-api (FastAPI), risk-engine (Go), notifier (Python), PostgreSQL, Redis | Small but realistic |
| IaC | Terraform: kind locally, EKS + VPC modules for cloud | One codebase, two targets; S3 remote state with locking |
| CI | GitHub Actions, reusable workflows | Free for public repos (GitLab CI) |
| Code security | Semgrep, Gitleaks, dependency review | Fail build on high findings |
| Image security | Trivy, Syft, Cosign keyless, SLSA provenance | Modern supply-chain standard |
| Registry | GHCR | Stores signatures/attestations next to images |
| GitOps | Argo CD app-of-apps + Kustomize overlays | Git is the source of truth (Flux) |
| Progressive delivery | Argo Rollouts canary + Prometheus analysis | Automatic promote/rollback (Flagger) |
| Admission policy | Kyverno | Readable YAML policies (OPA Gatekeeper) |
| Secrets | External Secrets Operator + AWS Secrets Manager (Vault dev locally) | No secrets in Git/images |
| Telemetry | OpenTelemetry SDK + Collector | Vendor-neutral |
| Observability | Prometheus, Loki, Tempo, Grafana | Metrics, logs, traces linked |
| SLOs | Sloth | Alerts on user impact |
| Chaos | Chaos Mesh | Repeatable failure injection |
| AIOps assistant | Python FastAPI + LLM with structured JSON | Reads Prometheus, Loki, K8s events, Argo CD; acts via audited action API |
| ChatOps | Slack app with Approve/Reject | Human in the loop |
| Audit | PostgreSQL table + Grafana dashboard | Every decision explainable |

## Hard problems (each gets a decision note in docs/decisions/)

1. **Change-to-incident correlation:** align alert start with Argo CD syncs, Rollouts steps,
   config changes; score by time proximity and service dependency (trace service graph).
2. **Context budget:** keep warnings/errors in the window, collapse repeated log lines into
   templates with counts, top 3–5 anomalous metrics, one failing trace → evidence pack with IDs.
3. **Hallucination guardrails:** schema-validated JSON, every claim cites evidence IDs,
   confidence level, "unknown" if no evidence; invalid output → one retry → escalate.
4. **Safe automation:** allow-listed action catalogue in tiers; blast-radius and rate limits;
   dry-run mode; global kill switch.
5. **GitOps-consistent remediation:** lasting fixes via revert PR in `sentinel-gitops` (Argo CD
   would undo manual changes); canary abort via Rollouts API.
6. **Alert storms:** group by service + window → one incident, one diagnosis, one LLM call.
7. **Signature enforcement:** Kyverno verifies keyless signatures by OIDC identity (this repo's
   workflow); narrow exception policy for third-party images; promote by digest.
8. **Canary on a quiet cluster:** k6 synthetic traffic; minimum request count before analysis
   can decide.

## Chaos scenarios (run each 5×, `make chaos-eval`)

| # | Scenario | Injection | Correct diagnosis | Correct response |
|---|---|---|---|---|
| 1 | Bad release: errors | Flag returns 500 on 20% of orders | New order-api release | Canary fails → auto rollback (T1) |
| 2 | Bad release: latency | +400 ms delay | New risk-engine release | Canary rollback or revert PR |
| 3 | Memory leak | Leak until OOMKilled | New release; OOMKilled events | Roll back (T1) |
| 4 | DB outage | Chaos Mesh kills PostgreSQL | PostgreSQL, not the apps | Notify only; no rollback |
| 5 | Network latency | +300 ms order-api ↔ risk-engine | Network, not code | Notify with evidence; no rollback |
| 6 | Bad config | Wrong Redis address via ConfigMap | The config change | Revert PR with approval (T2) |
| 7 | Traffic surge | k6 triples load | Capacity, not a fault | Scale-out proposal with approval (T2) |
| 8 | Unsigned image | Image built outside pipeline | Blocked at admission | Kyverno rejects |

Scenarios 4, 5, 7 test that the assistant does **not** blame the latest deploy.

Metrics: MTTD, MTTR, diagnosis accuracy (top-1), correct action rate, harmful actions (target 0),
cost per incident; compared with a baseline with the assistant off. Publish only measured numbers.

## Repo structure

```
sentinel/
├── README.md  Makefile  CLAUDE.md
├── apps/{order-api,risk-engine,notifier}/
├── aiops/{src/assistant/{grouping,collector,correlator,diagnosis,actions,slack}.py,tests}/
├── .github/workflows/{ci.yml,supply-chain.yml,promote.yml}
├── infra/terraform/{modules/{vpc,eks,irsa},envs/{local-kind,aws-demo}}
├── policies/kyverno/
├── observability/
├── chaos/   load/
└── docs/{PLAN.md,PROGRESS.md,architecture.md,decisions/,runbooks/}

sentinel-gitops/
├── bootstrap/          # Argo CD app-of-apps
├── platform/           # Kyverno, Rollouts, ESO, observability
└── apps/<service>/{base,overlays/{dev,staging,prod}}
```

## Phases

1. **Foundations (27 Oct – 2 Nov):** repo skeleton, 3 services with OTel, Dockerfiles, kind via
   Terraform, `make up`. Done when the app runs locally with one command.
2. **Secure CI (3 – 9 Nov):** reusable workflows, Semgrep, Gitleaks, Trivy, Syft, Cosign,
   provenance, GHCR. Done when every merge yields a signed, scanned image with SBOM.
3. **GitOps, policy, canary (10 – 16 Nov), v1 ships:** Argo CD, Kustomize overlays, promotion PRs
   with digests, Kyverno signature checks, Argo Rollouts canary. Done when a bad release rolls
   itself back and an unsigned image is refused.
4. **Observability and SLOs (17 – 23 Nov):** OTel Collector, Prometheus, Loki, Tempo, Grafana,
   Sloth, k6. Done when you can jump alert → trace → logs.
5. **AIOps read-only (24 – 30 Nov):** grouping, collector, correlator, evidence pack, LLM
   diagnosis with validation, Slack summary; script all 8 chaos scenarios.
6. **Guarded actions + evaluation (1 – 7 Dec):** action tiers, Slack approvals, revert PRs, audit
   log, dry-run, kill switch; full evaluation vs baseline.
7. **Polish + cloud demo (8 – 14 Dec):** EKS apply/record/destroy, README, 8 decision notes, demo
   video, launch post.
