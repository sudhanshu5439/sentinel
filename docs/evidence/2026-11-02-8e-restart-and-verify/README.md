# 8e: incidents survive a restart; the SLI is checked after an action (2026-11-02)

Laptop lab (kind), staging. Script: [restart-test.sh](restart-test.sh).

## The gap

Until now every deploy of the assistant forgot its incidents. In the 8b run 3 log, Alertmanager's
"resolved" for an incident opened before a restart came back as `resolved: ignored`; a still-firing
alert re-sent after a restart would have looked new and been investigated, and paid for, again.

## The fix

At start-up the assistant rebuilds the last 24 h of incidents from its own append-only audit log:
the `diagnosis` row (alerts, evidence pack, diagnosis), the latest `decision`/`action`/`approval`
row, and a new `resolved` event written when every alert of an incident has resolved. Tier 2
approval waits (an open revert PR) resume too. A row that does not parse is skipped and logged;
start-up never fails on the audit log.

## Live run

| Time (UTC) | Event |
|---|---|
| 05:35:38 | DB outage in staging (orders table renamed) → SLO alert via Prometheus/Alertmanager → incident opened |
| 05:35:46 | `dependency` / medium, notify only, posted to Slack, $0.0008 (audit rows 11–12) |
| 05:36:12 | table restored; **assistant pod restarted** while the incident was still open |
| 05:36:25 | `restored 1 incidents from the audit log (1 open)`: diagnosis and decision intact, no new LLM call |
| 05:45:38 | Alertmanager's resolved notification: `resolved: resolved` (before the fix: `ignored`); audit row 13 `resolved` |

Files: [db-run.txt](db-run.txt), [db-incident.json](db-incident.json),
[after-restart-log.txt](after-restart-log.txt), [after-restart-incident.txt](after-restart-incident.txt),
[audit.txt](audit.txt).

## Verifying an action

After an executed (tier 1) or approved (tier 2, PR merged) action, the assistant polls the
service's 5xx ratio every 30 s: two healthy readings in a row (< 1 %) = recovered; still failing
after 10 minutes = escalate to a human in Slack. No data is reported as "could not verify", never
as healthy. Audited as a `verify` event. The query was checked against the real Prometheus (both
environments read 0.0 at the time). It runs only after a live action, so it has no live run yet:
actions are still dry-run until sentinel-gitops PR #19 is merged.
