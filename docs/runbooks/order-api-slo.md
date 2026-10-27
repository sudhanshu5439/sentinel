# Runbook: order-api SLO alerts

Alerts: `OrderApiAvailabilityBudgetBurnFast|Slow`, `OrderApiLatencyBudgetBurnFast|Slow`
(defined in `sentinel-gitops/platform/observability/slo-order-api.yaml`).

| SLO | Target | Error budget (30 days) |
|---|---|---|
| Availability: POST /orders without 5xx | 99.5% | 0.5% of orders |
| Latency: POST /orders within 250 ms | 99% | 1% of orders |

**Fast** = spending budget ~14× too fast (1h and 5m windows): act now.
**Slow** = ~6× too fast (6h and 30m windows): investigate today.

## 1. Look (2 minutes)

1. Grafana → **Sentinel / Overview**, pick the environment from the alert's `namespace`.
2. Is a rollout in progress? Check the **Canary vs stable error rate** panel, and
   `make canary-watch ENV=<staging|prod>`. If only the canary is bad, Argo Rollouts should already
   be aborting it.
3. What changed recently? `sentinel-gitops` history: promotions (`promote(...)`) and
   chaos/config commits (`chaos(...)`).
4. Logs: the dashboard's logs panel, or Explore → Loki:
   `{namespace="sentinel-staging", app="order-api"} | json | level="error"`.
   Click a `trace_id` to open the trace in Tempo and see which call was slow or failed
   (risk-engine, PostgreSQL, Redis).

## 2. Act

| Situation | Action |
|---|---|
| A recent promotion or config change | Revert that commit in `sentinel-gitops`; Argo CD rolls back |
| Canary still running and bad | It aborts on its own; if not: `kubectl argo rollouts abort order-api -n <ns>` |
| risk-engine slow or down | Check its pods and logs; order-api returns 503 while it is down |
| PostgreSQL or Redis down | Check the pods and `/readyz` of order-api; restore the dependency |
| Injected faults (`FAULT_*` not 0) | `make canary-reset ENV=<env>` |

## 3. Afterwards

Write down what happened, how it was detected, and how long recovery took. The AIOps assistant
(next step) will draft this automatically.
