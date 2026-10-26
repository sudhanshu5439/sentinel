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

### 3. Secure CI (GitHub Actions)
- `ci.yml` on every PR and push to main: Gitleaks (full history), Semgrep (Python, Go, Dockerfile,
  Kubernetes rules; blocks on ERROR findings; results in the Security tab), ruff + pytest for the
  Python services, gofmt + vet + race tests for Go, kustomize + kubeconform for manifests.
- `supply-chain.yml` (reusable, one run per service): build, Trivy scan BEFORE publishing (blocks
  on fixable CRITICAL; HIGH+CRITICAL report to the Security tab), push to GHCR (only on main),
  SPDX SBOM (Syft), keyless cosign signature, SBOM attestation and SLSA build provenance.
- Least-privilege `permissions:` per job; OIDC instead of stored keys; Dependabot for actions,
  Python, Go and base images.
- `make verify SVC=order-api` checks the signature and attestations locally.
- Notifier logs switched to clean JSON (same shape as order-api).

## Next

- [x] First pipeline runs: Semgrep caught a script-injection pattern in our own workflow (fixed by passing
      context values via env); trivy-action pinned to a commit SHA after its old tag stopped resolving.
      Run 40307469006 is fully green: 3 images published, signed, with SBOM + provenance attestations.
- [ ] Make the 3 GHCR packages public (or add an image pull secret) so the cluster can pull them
- [ ] 4. GitOps + policy: sentinel-gitops repo, Argo CD, promotion PRs by digest, Kyverno

## Open questions / notes

- Steps 1–2 verified on the laptop: `make up` + `make smoke` passed on 26 Oct 2026.
- Actions are pinned to version tags; pinning to commit SHAs is a later hardening step.

## Session log

| Date | What was done | What's next |
|---|---|---|
| 26 Oct 2026 | Starter pack; demo apps with tests; Dockerfiles; kind via Terraform; manifests; Makefile | First `make up` on the laptop |
| 26 Oct 2026 | Smoke test passed on laptop; secure CI pipeline + supply chain | First pipeline run, GitOps |
