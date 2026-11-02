#!/usr/bin/env bash
# 8e live check: an incident survives an assistant restart. DB outage in staging (automatic
# alert path) -> incident diagnosed -> table restored -> assistant pod restarted mid-incident ->
# the incident is rebuilt from the audit log and Alertmanager's "resolved" closes it.
set -uo pipefail
EV=$(cd "$(dirname "$0")" && pwd)
NS=sentinel-staging
inc() { kubectl -n aiops port-forward svc/sentinel-assistant 18004:8000 >/dev/null 2>&1 & p=$!; sleep 3
        curl -s localhost:18004/incidents; kill $p; }
bash ~/projects/sentinel/docs/evidence/2026-10-30-8b-guarded-actions-dry-run/e2e-8b.sh db > "$EV/db-run.txt" 2>&1
tail -22 "$EV/db-run.txt"
id=$(jq -r .id ~/projects/sentinel/docs/evidence/2026-10-30-8b-guarded-actions-dry-run/db-incident.json)
mv ~/projects/sentinel/docs/evidence/2026-10-30-8b-guarded-actions-dry-run/db-* "$EV/"
echo "--- $(date -u +%T) restarting the assistant while $id is open"
kubectl -n aiops rollout restart deploy/sentinel-assistant
kubectl -n aiops rollout status deploy/sentinel-assistant --timeout=180s
sleep 5
kubectl -n aiops logs deploy/sentinel-assistant | grep '"msg"' | jq -r '.ts + "  " + .msg' | grep -v 'GET /' | tee "$EV/after-restart-log.txt"
inc | jq --arg id "$id" -r '.[] | select(.id == $id) | "after restart: \(.id) status=\(.status) diagnosis=\(.diagnosis.diagnosis.category) decision=\(.decision.action)/\(.decision.status)"' | tee "$EV/after-restart-incident.txt"
echo "--- waiting for Alertmanager's resolved notification"
for i in $(seq 1 60); do
  s=$(inc | jq --arg id "$id" -r '.[] | select(.id == $id) | .status')
  echo "$(date -u +%T) $s"; [ "$s" = resolved ] && break; sleep 30
done
kubectl -n aiops logs deploy/sentinel-assistant | grep '"msg"' | jq -r '.ts + "  " + .msg' | grep -v 'GET /' | tail -5 | tee -a "$EV/after-restart-log.txt"
kubectl -n aiops exec audit-db-0 -- psql -U audit_admin -d audit -c \
  "SELECT id, at, event, category, action, status FROM audit_events WHERE incident_id = '$id' ORDER BY id" | tee "$EV/audit.txt"
