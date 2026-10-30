# 8b live runs: policy-chosen actions in dry-run, with an append-only audit log (2026-10-30)

Laptop lab (kind), staging. The assistant runs in-cluster from the CI-signed image with
`ACTION_MODE=dry-run` set in sentinel-gitops: the policy decides, the guards run and everything
is audited, but nothing in the cluster or in Git is changed by the assistant. Single runs, not a
benchmark (that is Step 9). Scripts: [e2e-8b.sh](e2e-8b.sh), [abort-then-db.sh](abort-then-db.sh).

| Run | Fault | Alert path | Diagnosis | Policy decision | Right? |
|---|---|---|---|---|---|
| 1 | bad config release (50 % HTTP 500), canary in flight | handed to the webhook* | `config_change` / high, cites the Argo CD sync and the new ConfigMap | `abort_canary`, tier 1, **dry_run** | yes: Argo Rollouts' own analysis aborted the same canary 13 s later |
| 2 | DB table renamed, 13 min after run 1's aborted canary | fully automatic | `config_change` / **medium**, "config link unverified" | `revert_commit 4084022`, **tier 2 (human approval)**, dry_run | **no**: the cited canary was already aborted (0 replicas) |
| 3 | run 2's trap rebuilt on purpose (release → abort → reset → DB outage), after the fix | fully automatic | `dependency` / medium, cites the app exception and the Postgres log | `notify`, tier 0 | yes |

\* A 2-minute canary cannot move a 1-hour burn-rate window, so Alertmanager would not fire in
time; the same payload shape was posted to the in-cluster webhook. Everything after the webhook
ran as in production.

Costs: run 1 $0.002194 (two attempts, see below), run 2 $0.001117, run 3 $0.000691
(`claude-haiku-5-5`).

## What the runs found

1. **Run 1: citations with descriptions.** Attempt 1 cited `"[E1] new Rollout revision …"`
   instead of `"E1"`. `validate()` rejected it and the retry was valid, but the incident cost
   twice as much. Fix: every request's output schema now limits `evidence` to an enum of that
   pack's IDs, so constrained decoding cannot produce a citation that does not exist
   (`validate()` still checks). Verified with a real API call before deploying: 1 attempt.
2. **Run 2: a stale change blamed, and the guardrails held.** The 30-minute lookback still
   held run 1's config release. Its ReplicaSet had **0 replicas** (aborted) and the stable
   revision had not changed since 28 Oct, but neither the model nor the policy was told. The
   model blamed the config at *medium* confidence, so the policy put the revert at **tier 2**:
   live, it would have waited for a human, who would have read "config link unverified". In
   dry-run nothing happened. Reverting would also have been pointless: the commit had already
   been undone by the reset. Fix, in three places:
   - collector: each revision and ConfigMap carries `live` (replicas > 0 / used by a running
     revision), and the evidence text says "no longer running (0 replicas)"
   - correlator: a change that is not running keeps 0.2 of its weight, and when *no* change
     before the incident is still running, the explicit note says so
   - policy: only a change known to be running is aborted or reverted (unknown = no action)
3. **Run 3: the same trap, answered correctly.** The pack's first item was "no change before
   the incident is still running"; the model answered `dependency` and the policy notified only.
   That is the behaviour chaos scenario 4 (DB outage → "notify only; no rollback") requires.

## The audit log

Every run wrote two rows, `diagnosis` then `decision`, with the same prompt and pack hashes
([run1](run1-release-audit.txt), [run2](run2-misattributed-audit.txt),
[run3](run3-after-fix-audit.txt)). The `diagnosis` row's payload holds the alerts, the full
evidence pack (with the cluster facts the policy read) and the validated model output; the
`decision` row holds the action, tier, status, reasons and the guard that stopped it, if any.

[security-checks.txt](security-checks.txt), run against the live database:

| Check | Result |
|---|---|
| assistant role: INSERT, SELECT | allowed |
| assistant role: UPDATE, DELETE, TRUNCATE, CREATE TABLE, DROP TABLE | all refused |
| owner role: UPDATE, DELETE, TRUNCATE | refused by the trigger (`audit_events is append-only`) |
| Grafana role: SELECT / INSERT | allowed / refused |
| connect from a pod in another namespace | no response (NetworkPolicy, enforced by kindnet) |
| connect from the assistant pod | connected |
| assistant ServiceAccount: patch rollouts, create pods, get secrets | all `no` (still read-only) |

The checks inserted one test row (`incident_id = proof-8b`). It cannot be deleted: that is the
point. Limitation, stated plainly: `audit_admin` is the database superuser and could drop the
trigger; its password exists only in the `audit-db` Secret and the database container, and no
workload connects with it.

## Rules this step put in place

- The model chooses nothing: category + confidence + cited IDs go into `decide()`, plain code.
- Targets (rollout name, Git revision) come from cluster facts on the cited items, never from
  model text, so an injected "revert commit deadbeef" has nothing to attach to.
- Tiers: 0 notify · 1 act (staging, high confidence, or abort a canary in flight) · 2 a human
  approves (prod, medium confidence, suspected injection, scale-out) · tier 3 does not exist.
- Guards: actions kill switch (`make actions-off`), blast radius (own service, allowed
  namespaces), 3 actions per hour, 30-minute cool-down per service, dry-run by default (dry
  runs count against the limits, so they predict live behaviour).
- No audit, no action: a live action whose record cannot be written is blocked.
