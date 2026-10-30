# 7f live runs: alert → assistant → Haiku → Slack, in the cluster (2026-10-30)

Laptop lab (kind). The assistant runs in the `aiops` namespace from a CI-built, signed image
admitted by Kyverno, with read-only RBAC. The fault is a real dependency failure, not a release:
the staging `orders` table renamed (`ALTER TABLE orders RENAME TO orders_chaos`), restored
automatically by the script ([e2e-7f.sh](e2e-7f.sh)). Single runs, not a benchmark (Step 9).

| Run | Path | Outcome | Diagnosis | Cost |
|---|---|---|---|---|
| 1 | fully automatic | Alertmanager → assistant → Haiku worked; **nothing reached Slack** | `unknown` / low | $0.0006 |
| 2 | fully automatic, notifier fix deployed | **posted to Slack** | `unknown` / low | $0.000642 |
| 3 | log-collector fix deployed; alert handed in by hand (see below) | posted to Slack | **`dependency` / medium**: "queries a Postgres table `orders` that does not exist" | $0.000707 |

## Run 2 timeline (nothing sent by hand)

From [run2-assistant-log.txt](run2-assistant-log.txt) and the script output (UTC):

| Time | Event |
|---|---|
| 07:24:11 | table renamed: every `POST /orders` fails |
| ~07:24:50 | `OrderApiAvailabilityBudgetBurnSlow` firing (the 6 h window still held run 1's errors) |
| 07:25:08 | Alertmanager webhook → incident opened in the assistant |
| 07:25:09 | 10 evidence items → pack of 10 (~382 tokens) |
| 07:25:14 | diagnosis (1 attempt, 1986+886 tokens) and **posted to Slack** |
| 07:25:34 | table restored |

## What the runs found (all fixed, with tests)

1. **Run 1: the notifier was never called.** `create_app` built it, but the background
   investigation was started without it. No error could be logged, because nothing failed:
   the call simply never happened. Unit tests covered the notifier and the pipeline separately,
   not the wiring between them. Fix: pass it, plus a webhook-to-notifier regression test, plus a
   done-callback that logs any exception from a background investigation
   ([run1-incident-notifier-bug.json](run1-incident-notifier-bug.json): `notified_at: null`,
   `notify_error: null`).
2. **Runs 1–2: the error logs never reached the model.** Loki held
   `psycopg.errors.UndefinedTable: relation "orders" does not exist`, but the collector selected
   only streams with a `level` label; tracebacks and driver exceptions are plain text. The pack
   had 0 log lines, and the model said so in `missing_evidence` ("No order-api error logs")
   instead of guessing: an honest `unknown`. Fix: match on line content
   (`|~ "(?i)(error|exception|traceback|…|does not exist)"`), verified against the real Loki.
3. **Run 3** ([run3-after-log-fix.json](run3-after-log-fix.json)): 22 pack items, 12 of them
   log templates, including the order-api exception (×12) and the Postgres server log
   (`ERROR: relation "orders" does not exist`). Diagnosis `dependency`, citing both. Because
   Alertmanager does not re-send a still-firing alert within `repeat_interval`, this run's
   alert was posted to the in-cluster webhook by hand (same payload shape); everything after
   the webhook ran as in production.

## Still weak (next)

- Python traceback frames (`File ".../starlette/..."`) take about half of the log items: fold
  multi-line tracebacks into their final exception line.
- Metric baselines read "n/a" on runs 1–2 ("1h earlier" had no data at that moment); check the
  offset query against the Prometheus volume's history.
