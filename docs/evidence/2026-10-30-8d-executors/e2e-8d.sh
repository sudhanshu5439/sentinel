#!/usr/bin/env bash
# 8b live runs in staging, ACTION_MODE=dry-run (set in sentinel-gitops). Two scenarios:
#   release  a bad config release through Git (make canary-bad); while the canary is still in
#            flight, the burn-rate alert is handed to the in-cluster webhook (a 2-minute canary
#            cannot move a 1 h burn-rate window, so Alertmanager would not fire in time)
#   db       the staging orders table renamed (a dependency failure, no deploy); fully automatic,
#            the alert comes from Prometheus via Alertmanager. Always restored.
# Writes <scenario>-incident.json and <scenario>-audit.txt next to this script.
set -uo pipefail
scenario=${1:?release|db}
EV=$(cd "$(dirname "$0")" && pwd)
NS=sentinel-staging
cd ~/projects/sentinel
pf() { kubectl -n aiops port-forward svc/sentinel-assistant 18003:8000 >/dev/null 2>&1 & echo $!; }
phase() { kubectl -n $NS get rollout order-api -o jsonpath='{.status.phase} canary={.status.currentPodHash} stable={.status.stableRS}'; }

case "$scenario" in
release)
  echo "$(date -u +%T) before: $(phase)"
  make -s canary-bad ENV=staging RATE=0.5
  for i in $(seq 1 30); do
    p=$(phase); echo "$(date -u +%T) $p"
    set -- $p
    [[ "$1" =~ Progressing|Paused ]] && [ "${2#canary=}" != "${3#stable=}" ] && break
    sleep 5
  done
  sleep 40   # let the canary serve errors long enough to show in metrics and logs
  echo "$(date -u +%T) in flight: $(phase); handing the alert to the webhook"
  p=$(pf); sleep 3
  start=$(date -u -d '-1 min' +%Y-%m-%dT%H:%M:%SZ)
  curl -sf -X POST localhost:18003/webhook/alertmanager -H 'content-type: application/json' -d @- <<EOF
{"version":"4","status":"firing","alerts":[{"status":"firing",
 "labels":{"alertname":"OrderApiAvailabilityBudgetBurnFast","service":"order-api","namespace":"$NS","severity":"critical","slo":"order-api-availability"},
 "annotations":{"summary":"order-api is burning its availability error budget fast in $NS"},
 "startsAt":"$start","endsAt":"0001-01-01T00:00:00Z","fingerprint":"8b-release-$(date +%s)"}]}
EOF
  echo
  ;;
db)
  q() { kubectl -n $NS exec postgres-0 -- psql -U sentinel -d sentinel -tAc "$1"; }
  restore() { echo "$(date -u +%T) RESTORE: $(q 'ALTER TABLE IF EXISTS orders_chaos RENAME TO orders' 2>&1)"; }
  trap restore EXIT
  echo "$(date -u +%T) BREAK: $(q 'ALTER TABLE orders RENAME TO orders_chaos')"
  AM() { kubectl -n monitoring exec alertmanager-kps-alertmanager-0 -c alertmanager -- wget -qO- http://localhost:9093/api/v2/alerts 2>/dev/null; }
  for i in $(seq 1 60); do
    a=$(AM | jq -r '[.[] | select(.labels.namespace=="sentinel-staging") | "\(.labels.alertname)=\(.status.state)"] | join(" ")')
    n=$(kubectl -n aiops logs deploy/sentinel-assistant --since=10m 2>/dev/null | grep -c 'decision ')
    echo "$(date -u +%T) alerts: ${a:-none}  decisions: $n"
    [ "$n" -ge 1 ] && break
    sleep 20
  done
  restore; trap - EXIT
  p=$(pf); sleep 3
  ;;
esac

for i in $(seq 1 30); do
  n=$(curl -s localhost:18003/incidents | jq '[.[] | select(.decision != null and .notified_at != null)] | length')
  [ "${n:-0}" -ge 1 ] && break; sleep 2
done
curl -s localhost:18003/incidents | jq '.[0]' > "$EV/$scenario-incident.json"; kill $p
jq -r '"incident \(.id)",
  "diagnosis: \(.diagnosis.status) \(.diagnosis.diagnosis.category)/\(.diagnosis.diagnosis.confidence) attempts=\(.diagnosis.attempts) cost=$\(.diagnosis.cost_usd)",
  "summary:   \(.diagnosis.diagnosis.summary)",
  "cause:     \(.diagnosis.diagnosis.probable_cause.statement) \(.diagnosis.diagnosis.probable_cause.evidence)",
  "decision:  \(.decision.action) tier \(.decision.tier) \(.decision.status) target=\(.decision.target)",
  (.decision.reasons[] | "  - \(.)"),
  "audited=\(.audited) slack=\(.notified_at != null)"' "$EV/$scenario-incident.json"
id=$(jq -r .id "$EV/$scenario-incident.json")
kubectl -n aiops exec audit-db-0 -- psql -U audit_admin -d audit -c \
  "SELECT id, at, event, category, confidence, action, tier, status, left(prompt_sha256,16) AS prompt_sha, left(pack_sha256,16) AS pack_sha, cost_usd FROM audit_events WHERE incident_id = '$id' ORDER BY id" \
  | tee "$EV/$scenario-audit.txt"
kubectl -n aiops logs deploy/sentinel-assistant --since=15m | grep '"msg"' | jq -r '.ts + "  " + .msg' | grep -v 'GET /' | tail -8 | tee "$EV/$scenario-assistant-log.txt"
if [ "$scenario" = release ]; then
  echo "--- the platform's own safety net (Argo Rollouts analysis), independent of the assistant:"
  for i in $(seq 1 24); do p=$(phase); echo "$(date -u +%T) $p"; [[ "$p" == Degraded* || "$p" == Healthy* ]] && break; sleep 10; done
fi
