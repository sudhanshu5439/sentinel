# Progress log

## Done

### 1. Demo apps
- **order-api** (Python/FastAPI): `POST /orders` validates input, asks risk-engine, stores the order
  in PostgreSQL, publishes an event to the Redis Stream `orders`. Also `GET /orders/{id}`,
  `GET /orders`, `/healthz`, `/readyz`, Prometheus `/metrics`, JSON logs with trace ids, optional
  OpenTelemetry tracing, and fault-injection knobs (`FAULT_ERROR_RATE`, `FAULT_LATENCY_MS`) for
  the chaos scenarios. 12 unit tests.
- **risk-engine** (Go, standard library only): `POST /v1/check` applies pre-trade rules (side,
  positive values, max quantity, max notional, optional symbol allow-list). Graceful shutdown,
  JSON logs, a small built-in Prometheus exporter, `FAULT_LATENCY_MS`. Table-driven tests, race
  detector on.
- **notifier** (Python): Redis consumer group reads `orders`, sends a notification (a log line for
  now), acks every event (poison messages are logged and acked). `/healthz`, `/readyz`, `/metrics`.
  3 tests using fakeredis.

### 2. Containers + local cluster
- Multi-stage Dockerfiles: Python images built with uv from the lock file and run as UID 10001;
  Go binary is static on distroless `nonroot`.
- Terraform (`infra/terraform/envs/local-kind`): kind cluster, 1 control plane + 2 workers, host
  ports 8080/8443 reserved for ingress later.
- Kustomize (`deploy/local`): namespace with Pod Security Admission `restricted`; PostgreSQL
  StatefulSet with a PVC; Redis; the 3 apps with requests/limits, probes, non-root,
  read-only root filesystem, dropped capabilities. DB password generated into a git-ignored file.
- `make up | down | status | smoke | reload | logs | test | lint`.

## Next

- [ ] Run `make up` and `make smoke` on the laptop, fix anything that differs from the cloud workspace
- [ ] 3. Secure CI pipeline (GitHub Actions)

## Open questions / notes

- Images couldn't be built in the cloud workspace (Docker Hub blocked there), so the first real
  `docker build` happens on the laptop.

## Session log

| Date | What was done | What's next |
|---|---|---|
| 26 Oct 2026 | Starter pack; demo apps with tests; Dockerfiles; kind via Terraform; manifests; Makefile | First `make up` on the laptop |
