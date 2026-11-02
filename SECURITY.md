# Security

Sentinel is a personal lab project, not a production service. It is built to show how a delivery
platform can be secured end to end, so security reports are welcome.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability").
Please don't open a public issue for anything exploitable.

## What protects this repo

| Stage | Control |
|---|---|
| Every PR and push | Gitleaks (full history), Semgrep SAST (fails on ERROR), lint + unit tests, manifest validation |
| `main` | Protected: changes land through a pull request with those checks green |
| Image build | Multi-stage, non-root, minimal base; Trivy scan; Syft SBOM |
| Signing | cosign keyless (Sigstore), identity = `supply-chain.yml` on this repo |
| Provenance | SLSA build provenance and SBOM attestations (GitHub attestations) |
| Deploy | Images referenced by digest only; Kyverno admits a pod only if every Sentinel image is signed by this pipeline |
| Dependencies | Dependabot version updates |

### Verify an image yourself

```bash
cosign verify ghcr.io/sudhanshu5439/sentinel-order-api@sha256:<digest> \
  --certificate-identity-regexp 'https://github.com/sudhanshu5439/sentinel/.github/workflows/supply-chain.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

gh attestation verify oci://ghcr.io/sudhanshu5439/sentinel-order-api@sha256:<digest> \
  --owner sudhanshu5439
```

Current digests are in `apps/overlays/*/` of
[sentinel-gitops](https://github.com/sudhanshu5439/sentinel-gitops).

## The incident assistant

The LLM assistant (`aiops/`) treats everything the model reads and says as untrusted. Its
threat model (prompt injection, secret leakage, query injection, runaway actions and cost) and
the control for each threat are in the
[README](README.md#ai-security-the-llm-is-an-untrusted-component). In short:
evidence is redacted before it leaves the cluster, the model's output is schema-locked, actions
are chosen by deterministic policy rather than by the model, its only write permission is aborting
one canary in staging, and every decision is written to an append-only audit log.

## Secrets

No secrets are committed. Local secrets live in a git-ignored `.env`; the cluster gets them as
Kubernetes Secrets created by `make aiops-secrets`.
