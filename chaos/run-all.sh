#!/usr/bin/env bash
# Run every scenario RUNS times (default 5), one after another:  chaos/run-all.sh [runs] [scenarios...]
# Then chaos/summarize.py turns chaos/results/<date>/*.json into a table.
set -uo pipefail
cd "$(dirname "$0")"
runs=${1:-5}; shift || true
scenarios=${*:-bad-release-errors bad-release-latency prompt-injection unsigned-image db-outage}
for r in $(seq 1 "$runs"); do
  for s in $scenarios; do
    echo "=== $(date -u +%T) $s run $r"
    bash eval.sh "$s" "$r"
    sleep 150   # past the 2-minute incident window; eval.sh also waits for a clean staging
  done
done
echo "=== done $(date -u +%T)"
