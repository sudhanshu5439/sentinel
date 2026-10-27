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

### 4. GitOps + policy
- New repo `sentinel-gitops`: Argo CD app-of-apps (`bootstrap/root.yaml` → `argocd/`), Kyverno
  v1.19.2 from its pinned Helm chart (3.9.2, sized for a laptop), Sentinel policies, and the apps
  as `apps/base` + `apps/overlays/{staging,prod}` with images pinned by digest.
- Kyverno policies (new `policies.kyverno.io` CEL types; `ClusterPolicy` is deprecated in 1.19),
  enforced only in namespaces labelled `sentinel.io/require-signed-images=true`:
  - `verify-sentinel-images` (ImageValidatingPolicy): digest required + cosign keyless signature
    from `supply-chain.yml@refs/heads/main`, checked against Rekor. Fails closed.
  - `restrict-registries`: only Sentinel images plus postgres:16 / redis:7.
  - `require-resources-and-probes`: requests, memory limit, readiness probe.
- Policies are tested in the gitops repo's CI with the Kyverno CLI: real overlays must pass, known-bad
  fixtures must be denied (`scripts/validate.sh`).
- Promotion: after the supply chain, CI resolves the digests built from the commit, commits them to
  the staging overlay (Argo CD syncs), and opens a PR for prod. Needs the `GITOPS_TOKEN` secret.
- `make gitops-up | gitops-status | argocd-ui | policy-test`.

## Next

- [ ] Create the fine-grained `GITOPS_TOKEN` and add it as a secret in the sentinel repo
- [ ] Run `make gitops-up` on the laptop, then `make gitops-status` and `make policy-test`
- [ ] 5. Progressive delivery: Argo Rollouts canary with Prometheus analysis

## Open questions / notes

- Steps 1–2 verified on the laptop: `make up` + `make smoke` passed on 26 Oct 2026.
- Third-party actions use version tags except Trivy (pinned by SHA); full SHA pinning is a later hardening step.
- postgres and redis are still referenced by tag (Docker Hub isn't reachable from the cloud workspace
  to resolve digests). Pin them by digest from the laptop.
- Kyverno doesn't yet verify the SLSA provenance attestation (only the signature): next hardening step.
## Session log

| Date | What was done | What's next |
|---|---|---|
| 26 Oct 2026 | Starter pack; demo apps with tests; Dockerfiles; kind via Terraform; manifests; Makefile | First `make up` on the laptop |
| 26 Oct 2026 | Smoke test passed on laptop; secure CI pipeline + supply chain | First pipeline run, GitOps |
| 27 Oct 2026 | sentinel-gitops repo, Argo CD app-of-apps, Kyverno policies with tests, CI promotion | `make gitops-up` on the laptop |
