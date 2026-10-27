# CLAUDE.md: standing instructions for Claude Code

## What this project is

**Sentinel** is a self-healing, supply-chain-secured Kubernetes delivery platform with an
LLM-powered incident assistant. It's a public portfolio project owned by Sudhanshu Mishra
(GitHub: `sudhanshu5439`), a platform/DevOps engineer (CKA, AWS Security + Advanced Networking
Specialty, Terraform Associate) preparing for platform/SRE interviews at product companies.

The full design lives in `docs/PLAN.md`. Read it at the start of every session.
Current progress lives in `docs/PROGRESS.md`. Read it, and update it at the end of every session.

## The most important rule: teach, don't ghostwrite

Sudhanshu must be able to defend every line of this repo in an interview. So:

1. **Plan before code.** For any new piece of work, first explain what you will build, which files
   you'll create or change, and why. Then **stop and wait for his "go"**.
2. **Explain as you go.** After creating a file, give a short explanation of the key lines and the
   design choice (and the main alternative you didn't choose).
3. **Small steps.** One component at a time. Run it, verify it works, then commit. No giant
   multi-file dumps.
4. **Quiz occasionally.** After finishing a component, ask him one interview-style question about
   it ("What happens if…?", "Why X instead of Y?"). Don't give the answer unless he asks.
5. **Leave him things to do himself.** When a task is a good learning exercise (writing a Kyverno
   policy, a PromQL query, a decision note), offer to let him write it first and you review.
6. **Decision notes are his.** For each hard problem in `docs/PLAN.md`, he writes the note in
   `docs/decisions/` in his own words; you review and suggest improvements.

## Honesty rules

- This is a personal lab. Never write README text or docs that imply it ran in production or at a
  client.
- No client names, client code or client configuration. Everything is built from scratch.
- Numbers in the README (MTTD, MTTR, accuracy, rollback times) must come from actual measured runs
  stored in `chaos/results/`. Never invent or estimate them.

## Environment

- Windows 11 laptop, 16 GB RAM, working inside **WSL2 Ubuntu**. Docker Desktop with the WSL2
  backend. The repo lives at `~/projects/sentinel` (Linux filesystem, not `/mnt/c`).
- WSL is capped at about 10 GB RAM. Keep the local stack lean:
  - one kind cluster: 1 control-plane + 2 workers
  - set resource requests/limits on everything; prefer single-replica, small-footprint settings
    for platform charts (Prometheus retention short, Loki single-binary mode, Tempo monolithic)
  - if memory gets tight, say so and propose what to turn off rather than silently degrading
- Check what's installed before using a tool; if something is missing, tell him the install
  command and why it's needed rather than installing system packages silently.

## Safety and cost guardrails

- Never commit secrets. Use `.env` (git-ignored) locally; External Secrets in-cluster.
- Ask before any command that deletes data, rewrites git history or force-pushes.
- AWS (cloud demo only): always confirm before `terraform apply`; always show the destroy command;
  remind him to destroy the same day.
- LLM API calls in the assistant must have a timeout, a max-token cap and a per-incident budget.

## Conventions

- **Repos:** `github.com/sudhanshu5439/sentinel` (this repo) and
  `github.com/sudhanshu5439/sentinel-gitops` (what Argo CD watches).
- **Images:** `ghcr.io/sudhanshu5439/sentinel-<service>`, deployed by digest, never `latest`.
- **Python:** 3.12, FastAPI, `uv` for dependencies, `ruff` for lint/format, `pytest` for tests,
  type hints everywhere.
- **Go:** current stable, standard library HTTP where possible, `go test`.
- **Containers:** multi-stage builds, non-root user, minimal base images, health endpoints
  (`/healthz`, `/readyz`), OpenTelemetry instrumentation.
- **Kubernetes:** Kustomize base + overlays; every workload has requests/limits, probes,
  `runAsNonRoot`, read-only root filesystem where possible.
- **Makefile is the front door:** `make up`, `make down`, `make status`, `make test`, plus new
  targets as the platform grows. Every target is documented in `make help`.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`), small and
  frequent. Write the message so it explains *why*.
- **Docs:** update `docs/PROGRESS.md` (what was done, what's next, open questions) at the end of
  each session.

## Build backlog (no phases or dates; build continuously in this order)

1. ✅ Demo apps: order-api, risk-engine, notifier + tests
2. ✅ Containers + local cluster: Dockerfiles, kind via Terraform, Kustomize, `make up`
3. ✅ Secure CI: tests, Semgrep, Gitleaks, Trivy, Syft SBOM, Cosign, provenance, GHCR
4. ✅ GitOps + policy: sentinel-gitops repo, Argo CD, promotion PRs by digest, Kyverno
5. ✅ Progressive delivery: Argo Rollouts canary with Prometheus analysis
6. ✅ Observability + SLOs: OTel Collector, Prometheus, Loki, Tempo, Grafana, Sloth, k6
7. AIOps assistant (read-only): grouping, evidence, correlation, LLM diagnosis, Slack
8. Guarded actions: action tiers, approvals, revert PRs, audit log, kill switch
9. Chaos evaluation: 8 scenarios, measured results
10. Cloud demo + polish: EKS apply/record/destroy, README, decision notes, video

`docs/PROGRESS.md` tracks what's done and what's next.
