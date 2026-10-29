# 7e live run: LLM diagnosis on a real bad release (2026-10-29)

Laptop lab (kind), assistant run locally against the cluster's Prometheus, Loki and Kubernetes
API. Model `claude-haiku-5-5`, per-incident budget $0.02, `max_tokens` 2000, timeout 30 s.
Two incidents in one run; one run, not a benchmark (the measured evaluation is Step 9).

| Incident | What really happened | Diagnosis | Attempts | Tokens (in+out) | Cost |
|---|---|---|---|---|---|
| `sentinel-staging/order-api` | `make canary-bad RATE=0.3`: same image, faults ConfigMap switched to 30 % injected 500s; canary aborted | `config_change` / high: "faults ConfigMap change (…c9gc88h72k → …5m8b668dd4) with the same image caused injected 500s" | 1 | 3144+610 | $0.000619 |
| `sentinel-prod/order-api` (control) | nothing: alert sent by hand, prod healthy, no change | `unknown` / low: "alert is not reproduced by the evidence… cause is unknown" | 1 | 1705+981 | $0.000661 |

Both answers passed validation on the first attempt: every claim cites IDs that exist in the
pack, and the `config_change` cause cites a CHANGE item (E1).

## What to look at

- [incidents.json](incidents.json): the full `GET /incidents` output: alerts, raw evidence,
  the redacted evidence pack (E1…En), and `diagnosis` with cost, attempts, request IDs and the
  prompt hash.
- [prompt-staging.txt](prompt-staging.txt), [prompt-prod.txt](prompt-prod.txt): the exact
  system and user prompt, rebuilt from the stored pack. Their SHA-256 matches the
  `prompt_sha256` recorded at call time, which is the property the Step 8 audit log relies on.

## Honest observations

- **Right cause, slightly over-read symptom.** The staging diagnosis lists startup/readiness
  probe failures as "consistent with the service not serving after the change". Those probe
  failures are normal while a new pod starts; the claim is cited and not false, but it is
  weaker evidence than presented. Candidate fix: label probe failures within N seconds of pod
  start as start-up noise in the pack.
- **The control case is the more important result.** With nothing wrong and no change, the
  model said "unknown, low confidence" and listed what it could not see, instead of inventing a
  cause. The deterministic "no workload or config change before the incident" item (7d) was
  cited first.
- `injection_suspected` was false for both: the run had no adversarial text. The injection
  scenario is chaos scenario #9 (Step 9).
