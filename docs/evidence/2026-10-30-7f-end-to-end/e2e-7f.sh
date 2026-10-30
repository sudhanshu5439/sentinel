#!/usr/bin/env bash
# 7f end to end, nothing sent by hand: a real dependency failure in staging (orders table renamed)
# -> SLO burn-rate alert -> Alertmanager -> in-cluster assistant -> Haiku -> Slack. Always restored.
OUT=/mnt/c/Users/ayush/AppData/Local/Temp/claude/--wsl-localhost-Ubuntu-home-sudhanshu-projects-sentinel/70ec13a9-2402-49ad-9b58-c9c2da180df9/scratchpad/e2e-7f
mkdir -p $OUT
NS=sentinel-staging
psql() { kubectl -n $NS exec postgres-0 -- psql -U sentinel -d sentinel -tAc "$1"; }
restore() {
  echo "$(date -u +%T) RESTORE: $(psql 'ALTER TABLE IF EXISTS orders_chaos RENAME TO orders' 2>&1)"
}
trap restore EXIT
echo "tables before: $(psql "select string_agg(tablename, ',') from pg_tables where schemaname='public'")"
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$T0 BREAK: $(psql 'ALTER TABLE orders RENAME TO orders_chaos')"
AM() { kubectl -n monitoring exec alertmanager-kps-alertmanager-0 -c alertmanager -- wget -qO- http://localhost:9093/api/v2/alerts 2>/dev/null; }
for i in $(seq 1 60); do
  a=$(AM | jq -r '[.[] | select(.labels.namespace=="sentinel-staging") | "\(.labels.alertname)=\(.status.state)"] | join(" ")')
  n=$(kubectl -n aiops logs deploy/sentinel-assistant --since=30m 2>/dev/null | grep -c 'posted to Slack')
  echo "$(date -u +%T) alerts: ${a:-none}  slack-posts: $n"
  [ "$n" -ge 1 ] && break
  sleep 20
done
TFIRE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
restore; trap - EXIT
echo "$TFIRE restored; collecting evidence"
kubectl -n aiops logs deploy/sentinel-assistant --since=40m | grep -v '"GET /' > $OUT/assistant.log
grep -E '"msg"' $OUT/assistant.log | jq -r '.ts + "  " + .msg' 2>/dev/null | cut -c1-260
kubectl -n aiops port-forward svc/sentinel-assistant 18000:8000 >/dev/null 2>&1 & pf=$!; sleep 3
curl -s localhost:18000/incidents > $OUT/incidents.json; kill $pf
jq -r '.[] | "=== \(.namespace)/\(.service) opened \(.opened_at) alerts=\([.alerts[].alertname]|join(","))",
  "diagnosis: \(.diagnosis.status) \(.diagnosis.diagnosis.category // "-")/\(.diagnosis.diagnosis.confidence // "-") attempts=\(.diagnosis.attempts) cost=$\(.diagnosis.cost_usd) notified=\(.notified_at) err=\(.notify_error)",
  "summary: \(.diagnosis.diagnosis.summary // "-")",
  "cause:   \(.diagnosis.diagnosis.probable_cause.statement // "-") \(.diagnosis.diagnosis.probable_cause.evidence // [])"' $OUT/incidents.json
echo "break=$T0 fired+posted<=$TFIRE"
