# 8c/8d executors: canary abort and revert PRs, in dry-run (2026-10-30)

Laptop lab (kind), staging, `ACTION_MODE=dry-run`. The executors run, but stop before the first
write: the canary abort reports the exact API call it would make; the revert planner makes its
read-only GitHub calls and reports the PR it would open, or why it refuses.

## 1. Revert planner against the real sentinel-gitops history

[revert-plans-against-real-repo.txt](revert-plans-against-real-repo.txt), unauthenticated,
read-only:

| Commit | What it is | Plan |
|---|---|---|
| `53f636d` | the bad-release chaos commit, already undone by a reset | **refused**: the overlay file has changed since (already undone?) |
| `ae598d9` | the latest change to the staging overlay | restore `apps/overlays/staging/kustomization.yaml` from its parent |
| `6501558` | a CI deploy commit for `platform/aiops` | **refused**: outside `apps/overlays/staging/` (blast radius) |

The first row is the 8b run-2 trap caught a second time, independently: even if the liveness
check had missed it, Git itself shows that commit is no longer what is deployed.

## 2. In-cluster dry run on a bad release

[e2e-8d.sh](e2e-8d.sh) (same script as 8b): a bad config release, alert handed to the webhook
while the canary was paused.

| Time (UTC) | Event |
|---|---|
| 12:25:57 | canary `597f44bc87` progressing (same image, faults config) |
| 12:26:40 | incident opened |
| 12:26:50 | `config_change` / high, **1 attempt** (the citation enum: no retry this time), $0.000999 |
| 12:26:50 | decision `abort_canary` tier 1, dry_run; executor: *would PATCH rollouts/order-api/status abort=true in sentinel-staging* |
| 12:26:50 | posted to Slack; audit rows 8–10 (`diagnosis`, `decision`, `action`) |
| 12:27:03 | Argo Rollouts' own analysis aborted the canary (13 s later) |

Files: [release-incident.json](release-incident.json), [release-audit.txt](release-audit.txt),
[release-assistant-log.txt](release-assistant-log.txt).

## Not live yet, on purpose

Going live is sentinel-gitops PR #19: `ACTION_MODE=live` with `ALLOWED_NAMESPACES` limited to
staging, and one new permission (`patch` on `rollouts/status`, `resourceNames: [order-api]`,
sentinel-staging only). It changes what software may do to the cluster without a human, so a
human merges it. Revert PRs additionally need a fine-grained GitHub token for sentinel-gitops
only; without it they fail loudly with the plan recorded.
