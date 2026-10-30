#!/usr/bin/env bash
# The situation that fooled run 2, recreated on purpose: a bad config release that Argo Rollouts
# aborts (its ReplicaSet scaled to 0), reset through Git, then a database outage minutes later.
# Correct answer: dependency, and no revert of the aborted release.
set -uo pipefail
EV=$(cd "$(dirname "$0")" && pwd)
NS=sentinel-staging
cd ~/projects/sentinel
phase() { kubectl -n $NS get rollout order-api -o jsonpath='{.status.phase}'; }
make -s canary-bad ENV=staging RATE=0.5
for i in $(seq 1 40); do p=$(phase); echo "$(date -u +%T) $p"; [ "$p" = Degraded ] && break; sleep 10; done
make -s canary-reset ENV=staging
for i in $(seq 1 40); do p=$(phase); echo "$(date -u +%T) $p"; [ "$p" = Healthy ] && break; sleep 10; done
kubectl -n $NS get rs -l app=order-api -o custom-columns=NAME:.metadata.name,CREATED:.metadata.creationTimestamp,REPLICAS:.status.replicas --sort-by=.metadata.creationTimestamp | tail -3
bash "$EV/e2e-8b.sh" db
