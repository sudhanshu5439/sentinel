# Progress log

## Current phase

**Phase 1: Foundations** (target: 27 Oct – 2 Nov 2026)

Goal: `make up` creates a kind cluster with Terraform and runs order-api, risk-engine and notifier
(plus PostgreSQL and Redis), each with health endpoints and OpenTelemetry instrumentation.

### Phase 1 checklist

- [ ] Repo skeleton, `.gitignore`, `Makefile` with `help`
- [ ] order-api (FastAPI): place/list orders, calls risk-engine, stores in PostgreSQL
- [ ] risk-engine (Go): approve/reject an order by simple rules
- [ ] notifier (Python): consumes order events from Redis, logs a notification
- [ ] Multi-stage, non-root Dockerfiles for all three
- [ ] Unit tests for each service
- [ ] Terraform `envs/local-kind`: 1 control plane + 2 workers
- [ ] Kustomize manifests (requests/limits, probes, securityContext)
- [ ] `make up` / `make down` / `make status` / `make test`
- [ ] Decision note 0001 (written by Sudhanshu): why kind + Terraform for local

## Session log

| Date | What was done | What's next | Open questions |
|---|---|---|---|
| 26 Oct 2026 | Starter pack added: CLAUDE.md, PLAN.md, PROGRESS.md | Phase 1 kickoff | |
