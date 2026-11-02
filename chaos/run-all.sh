#!/usr/bin/env bash
# Run every scenario RUNS times (default 5):  chaos/run-all.sh [runs] [first-run] [scenarios...]
# Scenario by scenario (all runs of one, then the next), db-outage last: a DB outage leaves the
# slow burn-rate alert firing for a long time, and a run started meanwhile would join it.
# Then chaos/summarize.py turns chaos/results/<date>/*.json into a table.
set -uo pipefail
cd "$(dirname "$0")"
runs=${1:-5}; first=${2:-1}; shift 2 || shift $#
scenarios=${*:-bad-release-errors bad-release-latency prompt-injection unsigned-image db-outage}
for s in $scenarios; do
  for r in $(seq "$first" "$runs"); do
    [ -f "results/$(date -u +%F)/$s-$r.json" ] && ! grep -q '"error"' "results/$(date -u +%F)/$s-$r.json" \
      && { echo "=== $s run $r: already done"; continue; }
    echo "=== $(date -u +%T) $s run $r"
    bash eval.sh "$s" "$r"
    sleep 150   # past the 2-minute incident window; eval.sh also waits for a clean staging
  done
done
echo "=== done $(date -u +%T)"
