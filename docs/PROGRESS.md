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

### 5. Progressive delivery (canary)
- Found and fixed a real bug first: order-api's fault-injection middleware was outermost, so injected
  500s and delays never reached Prometheus. A canary analysis could not have caught a bad release.
  Middleware order is now explicit and tested; metrics use a per-app registry.
- `order_api.loadgen`: steady synthetic traffic (5 orders/s), shipped in the signed order-api image.
- gitops repo: kube-prometheus-stack 92.14.0 (lean: no Grafana/Alertmanager/node-exporter yet) and
  Argo Rollouts 1.10.1, both via Argo CD.
- order-api is a Rollout with 4 replicas: 25 → 50 → 75 → 100 % with 2m/2m/1m pauses. Background
  AnalysisTemplate queries Prometheus every 30 s about the canary pods only (by pod-template hash):
  traffic ≥ 10 req/min, 5xx ≤ 5 %, p95 ≤ 300 ms. Second failure aborts.
- Fault switches are a generated ConfigMap per environment; a change in Git → new hash → new pod
  template → real canary. `make canary-bad | canary-slow | canary-reset | canary-watch`.
- First automatic promotion worked: CI committed staging and opened a prod PR. That PR conflicted
  with the canary restructuring, so it was replaced by PR #2 (same digests) instead of being force-pushed.

### 6. Observability + SLOs (sentinel-gitops PR #5, merged 27 Oct)
- Grafana + Alertmanager (kube-prometheus-stack), Loki 3.7.9 and Tempo 3.1.1 (now from the
  grafana-community charts), Grafana Alloy as the one agent: pod logs → Loki, OTLP traces → Tempo.
  Promtail is end-of-life, so Alloy replaces it; it is also an OpenTelemetry Collector distribution.
- Grafana datasources link logs and traces both ways (trace_id in a log line ↔ trace in Tempo).
- order-api exports traces (OTEL_EXPORTER_OTLP_ENDPOINT → Alloy).
- SLOs for POST /orders per environment: 99.5% availability, 99% within 250 ms. Multi-window,
  multi-burn-rate alerts (14.4× over 1h+5m, 6× over 6h+30m), unit-tested with `promtool test rules`:
  a bad release pages, a 1-minute blip doesn't, a healthy service never alerts.
- Dashboard "Sentinel / Overview" generated by `tools/gen_dashboard.py` (RED metrics, canary vs
  stable, burn rate, risk-engine, notifier, error logs).
- gitops CI now renders every Helm-based app with its real values (`scripts/render-helm.sh`).
  It caught a broken Tempo value before it reached the cluster.
- Runbook: docs/runbooks/order-api-slo.md. `make grafana-ui | alertmanager-ui`.

### Incident on the laptop (27–28 Oct): debugging story worth telling
- Symptoms: postgres 19 restarts (exit 0), notifier 26, Grafana killed at start-up (exit 137),
  kube-apiserver restarted 13× (`/livez` → 500), canaries aborted on `canary-traffic`.
- Path: probe timeouts → apiserver liveness → etcd slow reads (6k "took too long") → only
  ~11 req/s on the apiserver, so not load → **PSI**: memory `full avg60` 20.6, swap in use.
- Root cause: WSL had 7.6 GiB (default 50%), not the planned 10 GB. Fix: `.wslconfig`
  `memory=10GB, swap=4GB`. After: swap 0, pressure gone.
- Hardening kept: realistic probe timeouts + startupProbes (gitops PR #6), Grafana loads only the
  plugins it uses (PR #7).
- Second, separate cause of canary aborts: loadgen reused one keep-alive connection and a Service
  balances per connection, so canary pods got no traffic. Fixed with `Connection: close` (a923a5c).

### Platform UIs through an ingress (28 Oct, sentinel-gitops PRs #9–#12)
- Traefik 3.7 (chart 41.7.1) on the control-plane node's hostPorts 80/443, which kind maps to
  laptop 8080/8443. No more port-forwards: `http://<tool>.localtest.me:8080` for grafana,
  argocd, alertmanager, prometheus, rollouts, plus orders-staging / orders-prod (order-api
  Swagger UI). `make urls` probes them; `make grafana-ui` / `argocd-ui` print the password.
- Traefik, not ingress-nginx (retired March 2026). Rollout replaces in place (maxSurge 0): a
  hostPort fits once per node. Ingress status publishes 127.0.0.1 so Argo CD reports Healthy.
- Prometheus/Alertmanager externalUrl and Grafana root_url point at the ingress hosts, so alert
  links (generatorURL) open in a browser; the assistant will cite them.
- Lessons: (1) `service.type` vs the chart's `service.spec.type`: Helm ignored the unknown key,
  the Service stayed LoadBalancer `<pending>`, Argo CD never saw Healthy. Render and check the
  object you care about, not only the fields you expect. (2) Sync-wave deadlock: root waited
  for the unhealthy Traefik app before applying the commit that fixed it. Unblocked by applying
  the same file from Git by hand.
- Cluster recovered: kube-proxy `too many open files` fixed by inotify limits
  (`/etc/sysctl.d/99-kind.conf`, survives WSL restarts). Prod promoted (PR #8, a923a5c) and the
  canary completed: staging and prod both run the loadgen fix.
- Proof: `docs/screenshots/2026-10-28-ingress/` (terminal proof, Grafana, Argo CD, Alertmanager,
  Prometheus, Rollouts, order-api Swagger).

### 7. AIOps assistant (read-only), in progress
Built in slices: 7a grouping → 7b collectors → 7c evidence pack → 7d correlator → 7e LLM
diagnosis → 7f Slack + deploy.
- **7a done:** `aiops/` (one uv project, package `assistant`). `POST /webhook/alertmanager` parses
  the v4 payload; `IncidentGrouper` groups by (namespace, service) from the real SLO rule labels.
  An alert joins the incident if it is still open or was active in the last 15 min, duplicates
  (same fingerprint and status) are ignored, and only a new incident will trigger diagnosis.
  In-memory store capped at 500 incidents. `GET /incidents[/{id}]`. 14 tests; added to `make
  test/lint` and CI.
- **7b done:** collectors run once per new incident, in the background (the webhook answers
  Alertmanager immediately). Prometheus: 5xx ratio, p95, request rate for *every* app in the
  namespace now vs 1h earlier, restarts, terminations inside the window, unready pods. Loki:
  warning/error lines. Kubernetes API: Warning events, Argo CD Application history, Rollout
  conditions/phase (read as CRs: a read-only ServiceAccount, no Argo CD login). A failing or slow
  collector becomes a GAP item, never a failed incident. Namespace is validated before it goes
  into PromQL/LogQL. ServiceAccount token re-read per request (kubelet rotates it). 23 tests.
- Verified live (assistant on the laptop, `kubectl proxy` + Loki port-forward): one webhook →
  34 evidence items in ~1 s. It caught the WSL restart (all containers terminated ~10:00) and
  listed Argo CD sync 0edf5db, which was an ingress-only commit that did not change staging:
  proof that 7d must check whether a sync actually changed the app, not just that it happened.
- Prometheus has no PVC, so a restart loses history (baselines "n/a" right after a restart).
- **7c done:** evidence pack (`pack.py`). Redact (API keys, AWS keys, GitHub tokens, JWTs,
  bearer tokens, passwords in URLs and key=value, emails) → collapse (log lines templated:
  UUIDs, timestamps, IPs, hex, numbers → placeholders; events grouped by reason + message) →
  rank (gaps, changes newest first, most abnormal metrics, events, logs by count) → token
  budget (default 6000, gaps never dropped, dropped counts reported) → IDs E1..En. Each item is
  one line capped at 300 chars, so injected text cannot fake a new `[E2]` line. 15 new tests
  (38 total), including an injection case.
- Found live: with zero errors, the 5xx-ratio query returned *no series*, so a healthy app
  looked "unknown". Fixed with `or 0 * total`: healthy now reads 0.
- Anthropic API key in `.env` (git-ignored, mode 600); one test call to `claude-haiku-5-5`
  succeeded (22 in / 8 out tokens). The key was pasted into a chat once: rotate it.
- **7d done:** change correlation (`correlator.py`). Suspect score = time (e^(-min/15); after
  the incident start = 0.1, "a reaction") × service (alerting service 1.0, neighbour 0.5) ×
  kind (workload revision 1.0, config 0.9, rollout status 0.3, sync 0.3 if it produced a
  revision else 0.05). Each real change is credited to the *latest* sync before it only. When
  nothing changed, an explicit "no workload or config change" item leads the pack (the
  evidence scenarios 4, 5, 7 need). The pack sorts changes by score and shows the reasons.
- What counts as "the workload changed", learned from 5 live bad releases in staging:
  1. A new ReplicaSet, **diffed against the rollout's stable one**: image (code) vs referenced
     ConfigMaps (config). Needed because kustomize names ConfigMaps by content hash and keeps
     old ones (`Prune=false`): re-releasing a known config re-uses an old ConfigMap and
     creates no new object.
  2. A **re-deployed** existing ReplicaSet: Argo Rollouts re-uses the ReplicaSet of an
     identical template (repeated release, rollback), so nothing new is created either.
     Dated from its **AnalysisRun** (durable) and, best-effort, the "started update to
     revision N" event. Events alone failed: the event recorder rate-limits per object and
     silently dropped "revision 20" during rapid releases.
  3. An Argo CD sync alone proves nothing (any commit anywhere moves the revision).
- Final live run: E1 = "re-deployed revision 22 of order-api; image unchanged; config
  …c9gc88h72k → …5m8b668dd4", suspect 1.00, the right sync credited, the reset commit's sync
  0.04. 48 tests.
- Small fixes: error-ratio rules read 0 when healthy (Grafana "No data" gone; gitops PR #14,
  promtool tests still pass); Prometheus on a 2Gi volume; kind ports bound to 127.0.0.1 and
  `make cluster` refuses to rebuild a running cluster (that change forces replacement, and
  `apply -auto-approve` would have wiped Argo CD); old pre-GitOps `sentinel` namespace scaled
  to 0 (deleting it, with its volume, is left to Sudhanshu).
- RBAC the assistant will need in 7f (read-only): events, replicasets, configmaps (metadata),
  rollouts, analysisruns in the app namespaces; applications in argocd.
- **7e done:** LLM diagnosis (`diagnosis.py`, `llm.py`), `claude-haiku-5-5` via the official
  `anthropic` SDK (2.3). The model gets the pack inside `<evidence>` tags (a closing tag inside
  the data is defused) and a system prompt that calls the evidence untrusted data. Output is
  JSON-schema-constrained, then validated by code: every claim cites ≥1 ID that exists in the
  pack; `bad_release`/`config_change` must cite a CHANGE item; `unknown` cannot be high
  confidence; ≤6 supporting claims; output cleaned (redaction, control chars, length) before
  humans see it. Invalid → one retry with the errors → still invalid = `escalate` (never a
  guess). Guardrails: 30 s timeout, 1 SDK retry, `max_tokens` 2000, effort low, $0.02
  per-incident budget checked against the *worst case* before each call; unknown model price =
  refused; `LLM_ENABLED=false` kill switch (no key = off too). Prompt SHA-256, token counts,
  cost and request IDs stored on the incident (input for the Step 8 audit log). No tools, no
  commands: Step 8 maps the category to allow-listed actions. 13 new tests with a fake LLM (61
  total).
- Live run ([evidence](evidence/2026-10-29-7e-llm-diagnosis/README.md)): a real 30 %-errors
  config release in staging → `config_change`/high, cause cites E1 (same image, faults ConfigMap
  c9gc → 5m8b), 1 attempt, $0.0006. Control: a hand-sent alert for healthy, unchanged prod →
  `unknown`/low, "alert not reproduced by the evidence", $0.0007. Prompt hashes reproducible
  from the stored packs. Weak spot seen: start-up probe failures read as a symptom (they are
  normal while a pod starts).
- **7f done: Slack + in-cluster deploy + Alertmanager route.**
  - Slack app "Sentinel Assistant" created through the Slack API (manifest + OAuth), webhook
    into #sentinel-incidents; URL only in `.env` and the in-cluster Secret.
  - `notify.py`: one Block Kit message per incident (cause with its cited evidence quoted,
    supporting claims, what the assistant could not see, cost, "read-only: no action taken";
    escalations say a human is needed and why). Model/telemetry text escaped (`&<>` entities
    defuse `<!channel>`, `<@user>`, `<url|label>`; bare URLs defanged; `@here` broken),
    length-capped; Slack failures recorded on the incident, never raised.
  - Shipping: `aiops/Dockerfile`; CI builds/scans/signs/attests `sentinel-assistant` on its
    own path (aiops/ changes start no app canary) and commits the digest to sentinel-gitops
    `platform/aiops`. First build failed Trivy's *secret* scan on a docstring in the Anthropic
    SDK (vulnerabilities were 0): narrow allow-rule in `.github/trivy-secret.yaml`.
  - gitops PRs #15–#17: `aiops` namespace (Kyverno-enforced: unpinned image denied, shown with
    a server-side dry run), namespaced list-only Roles (verified with `kubectl auth can-i`: no
    Secrets, exec, writes or other namespaces), restricted pod (non-root, read-only root FS,
    no capabilities), Argo CD Application, Alertmanager route `slo=~".+"` → assistant.
  - Kill switch redesigned: `kubectl set env` would be reverted by self-heal, so the
    break-glass switch is an optional ConfigMap kept out of Git, re-read as a file before every
    diagnosis (`make aiops-off/on`). `make aiops-secrets`, `make aiops-status`.
- 7f live runs ([evidence](evidence/2026-10-30-7f-end-to-end/README.md)), staging `orders` table
  renamed (a dependency failure, no deploy):
  1. Automatic: Alertmanager → assistant → Haiku worked, nothing reached Slack: the notifier
     was never passed to the background task (fixed + regression test + exceptions in
     background tasks now logged).
  2. Automatic: break 07:24:11 → incident 07:25:08 → diagnosis + Slack 07:25:14. Still
     `unknown`: the log collector only selected streams with a `level` label, so the
     unlabelled `relation "orders" does not exist` never reached the model (which said so).
  3. After the log fix: `dependency`/medium, cause = missing Postgres table, citing the app
     exception and the Postgres log. $0.0007.
- Evidence pack: startup-probe failures labelled as start-up noise (7e weak spot).
- Found on the way: the first 7e live run lost both webhooks silently. The SDK import made the
  server start slower than the script's `sleep 4`, and `curl -s` hid the refusal. Wait on
  `/healthz` and fail loudly (also what a readiness probe is for).

### 8. Guarded actions, in progress

- **8a** (`9e9b8c8`): the two 7f weak spots.
  - Traceback frames are excluded in the LogQL query (`!~ "^\s|^Traceback …"`), so they stop
    using up the 200-line limit; the unindented exception line is kept. Checked against the
    real Loki: frames gone, the `UndefinedTable` line and the Postgres log stay.
  - The "n/a" baselines were not a query bug: Prometheus had no data an hour before because
    the laptop had been off (`count(up)` starts 06:40). With no 1h-earlier sample the value at
    the window start is used and labelled as such.
- **8b** (`d2fbce8`, `23ee4e1`, `a6fb60e`; gitops PR #18): actions chosen by policy code, in
  dry-run, with an append-only audit log
  ([evidence](evidence/2026-10-30-8b-guarded-actions-dry-run/README.md)).
  - `actions.py`: `decide()` maps a validated diagnosis to one action from a closed catalogue
    (notify · abort canary · revert the causing gitops commit · propose scale-out) with a tier
    (0 notify, 1 act, 2 human approves; tier 3 is not in the catalogue). Targets come from
    cluster facts the collectors put on the cited pack items, never rendered into the prompt,
    so model text cannot name a commit or a rollout. Guards: actions kill switch, blast radius,
    3/hour, 30-min cool-down per service, dry-run default (dry runs count, so they predict live).
  - `audit.py` + gitops `platform/aiops/audit-db.yaml`: PostgreSQL 16 (16 MB shared buffers,
    192Mi limit, pinned by digest, read-only root FS, NetworkPolicy). The assistant role has
    INSERT/SELECT only; a trigger refuses UPDATE/DELETE/TRUNCATE even for the owner; Grafana
    gets SELECT. Every incident writes `diagnosis` (pack, pack hash, prompt hash, output) and
    `decision` rows. No audit, no action. All of it checked against the live DB.
  - `make actions-off/on`, `make audit-log`; `aiops-secrets` generates the DB passwords once.
- 8b live runs in staging (dry-run):
  1. Bad config release, canary in flight → `config_change`/high → `abort_canary` tier 1,
     dry_run. Argo Rollouts' analysis aborted the same canary 13 s later. Attempt 1 cited
     `"[E1] description"` and cost a retry → output schema now limits citations to an enum of
     the pack's IDs (verified against the API).
  2. DB outage 13 min after that aborted canary → blamed on the canary's config at *medium*
     confidence → `revert_commit` at **tier 2** (human approval), dry-run: nothing happened, but
     it was wrong. The canary's ReplicaSet had 0 replicas. Fixed in the collector (`live`),
     the correlator (not-running changes ×0.2, explicit "no change is still running" note) and
     the policy (only running changes are undone).
  3. Same trap rebuilt on purpose after the fix → `dependency`/medium → notify only. Correct.

## Next

- [ ] Screenshot: Loki log line with trace_id → Tempo trace
- [ ] SLO alert test: rename `orders` table in staging → `OrderApiAvailabilityBudgetBurnFast` fires →
      rename back. (A bad canary can't trigger it: the canary aborts before the 1h window burns.)
      Capture the webhook body as a test fixture for the assistant.
- [ ] Decision note 0001 (WSL memory incident), written by Sudhanshu
- [ ] Step 5 + 6 explainer docs and LinkedIn posts (need screenshots)
- [x] **7. AIOps assistant (read-only)**: done (7a–7f), running in-cluster, posting to Slack
- [x] Pack: traceback frames out of the log query (8a)
- [x] Baselines "n/a": no data an hour earlier (laptop off); window-start fallback (8a)
- [x] 8b: policy-chosen actions, guards, append-only audit DB, dry-run live runs
- [ ] **8c/8d** ← next: executors. Abort a canary through the Rollouts status subresource (a
      new Role with `patch rollouts/status` only); revert = a PR in sentinel-gitops computed
      from the cited commit, refused if a later commit touched the same files or if it touches
      anything outside that environment's overlay. Tier 1 merges it, tier 2 waits for a human
      to merge it (the merge *is* the approval, recorded by GitHub). Needs a fine-grained
      GitHub token scoped to sentinel-gitops only (Sudhanshu creates it).
- [ ] 8e: after an action, watch the SLO for N minutes; not recovered → escalate. Postmortem
      draft as a GitHub issue. Grafana panel on the audit table.
- [ ] Incidents survive a restart (rebuild open ones from the audit log): today a restart
      makes the next "resolved" ignored (seen live in 8b run 3)
- [ ] Slack screenshot of the run-3 diagnosis for the README (Sudhanshu; the message holds no
      secrets, but check before committing)
- [ ] Delete the stopped pre-GitOps namespace when sure: `kubectl delete namespace sentinel`
- [ ] Kind port binding (127.0.0.1) applies on the next `make down && make up && make gitops-up`
- [ ] Rotate the Anthropic API key (it was pasted into a chat); store the new one with `read -s`
- [ ] Decision note 0006 (alert storms), written by Sudhanshu

## Open questions / notes

- Steps 1–2 verified on the laptop: `make up` + `make smoke` passed on 26 Oct 2026.
- Third-party actions use version tags except Trivy (pinned by SHA); full SHA pinning is a later hardening step.
- postgres and redis are still referenced by tag (Docker Hub isn't reachable from the cloud workspace
  to resolve digests). Pin them by digest from the laptop.
- Kyverno doesn't yet verify the SLSA provenance attestation (only the signature): next hardening step.
- LLM for 7e: decided: hosted `claude-haiku-5-5` behind an `LLM` protocol with a fake for tests;
  Ollama doesn't fit next to the stack in ~10 GB. ~$0.0006 per incident measured (one run).
- Known gap: an incident whose resolve notification never arrives stays open forever and swallows
  later alerts for that service. Revisit with durable storage (Step 8).
- `go` isn't on PATH in non-interactive WSL shells, so `make lint` stops at risk-engine there.
## Session log

| Date | What was done | What's next |
|---|---|---|
| 26 Oct 2026 | Starter pack; demo apps with tests; Dockerfiles; kind via Terraform; manifests; Makefile | First `make up` on the laptop |
| 26 Oct 2026 | Smoke test passed on laptop; secure CI pipeline + supply chain | First pipeline run, GitOps |
| 27 Oct 2026 | sentinel-gitops repo, Argo CD app-of-apps, Kyverno policies with tests, CI promotion | `make gitops-up` on the laptop |
| 27 Oct 2026 | Kyverno verified on laptop; metrics bug fixed; Prometheus + Argo Rollouts; canary with analysis | Canary experiment on the laptop |
| 27 Oct 2026 | Canary abort seen on laptop; prune and rebuild-on-every-commit bugs fixed; observability + SLOs as PR #5 | Merge PR #5, explore Grafana |
| 27–28 Oct 2026 | PR #5 merged; WSL memory incident found via PSI and fixed; probe + Grafana hardening; loadgen keep-alive bug fixed | Step 7: AIOps assistant |
| 28 Oct 2026 | Step 7 sliced; 7a: assistant skeleton, Alertmanager webhook, incident grouping, tests, CI | 7e LLM diagnosis |
| 28 Oct 2026 | Cluster recovered (inotify); Traefik ingress for all UIs + order-api; prod promoted; screenshots | 7b collectors |
| 28 Oct 2026 | 7b collectors; GitHub portfolio (READMEs, profile, topics, old repos private); AI-security positioning | 7c |
| 29 Oct 2026 | New architecture diagram; API key set up and tested; 7c evidence pack with redaction | 7d correlator |
| 29 Oct 2026 | 7d correlator, verified on 5 live bad releases; zero-error rules, Prometheus volume, loopback ports, safe `make cluster` | 7e LLM diagnosis |
| 29 Oct 2026 | 7e LLM diagnosis (Haiku, cited + validated, retry → escalate, budget, kill switch); live bad release + control run saved as evidence | 7f Slack + deploy |
| 30 Oct 2026 | 7f: Slack app via API, notifier, signed image via CI, aiops namespace + read-only RBAC + Alertmanager route; 3 live runs found 2 bugs (notifier never wired, unlabelled error logs missed), both fixed; final run diagnosed the DB failure correctly | Step 8 guarded actions |
| 30 Oct 2026 | 8a traceback/baseline fixes; 8b action policy + guards + append-only audit DB (PR #18) in dry-run; 3 live runs: canary abort proposed correctly, a stale aborted canary blamed (tier 2, nothing done) → liveness facts fixed it, rerun correct; citation enum in the schema | 8c/8d executors (needs a GitHub token) |
