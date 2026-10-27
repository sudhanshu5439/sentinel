#!/usr/bin/env bash
# Canary experiments the GitOps way: every change is a commit to sentinel-gitops, never kubectl.
#   canary.sh bad   <env> [error_rate]   inject errors into order-api (default 0.3 = 30%)
#   canary.sh slow  <env> [latency_ms]   inject latency into order-api (default 500 ms)
#   canary.sh reset <env>                back to healthy (0 errors, 0 ms)
#   canary.sh watch <env>                follow the rollout live
set -euo pipefail
cmd=${1:?usage: canary.sh bad|slow|reset|watch <staging|prod> [value]}
env=${2:?environment: staging or prod}
OWNER=${OWNER:-sudhanshu5439}
ns="sentinel-$env"

watch_rollout() {
  if kubectl argo rollouts version >/dev/null 2>&1; then
    kubectl argo rollouts get rollout order-api -n "$ns" --watch
  else
    echo "(tip: install the kubectl-argo-rollouts plugin for a richer view; see docs)"
    kubectl get rollout order-api -n "$ns" -w
  fi
}

set_faults() { # error_rate latency_ms message
  local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  gh repo clone "$OWNER/sentinel-gitops" "$tmp" -- --depth 1 -q
  local file="$tmp/apps/overlays/$env/kustomization.yaml"
  sed -i -E "s/FAULT_ERROR_RATE=[0-9.]+/FAULT_ERROR_RATE=$1/; s/FAULT_LATENCY_MS=[0-9]+/FAULT_LATENCY_MS=$2/" "$file"
  if git -C "$tmp" diff --quiet; then echo "nothing to change: $env already has error_rate=$1 latency_ms=$2"; return; fi
  git -C "$tmp" commit -qam "chaos($env): $3"
  git -C "$tmp" push -q origin HEAD:main
  echo "pushed: chaos($env): $3"
  echo "Argo CD picks it up within ~3 min (click Refresh in the UI to make it immediate)."
}

case "$cmd" in
  bad)   rate=${3:-0.3}; set_faults "$rate" 0 "order-api returns HTTP 500 for ${rate} of orders" ;;
  slow)  ms=${3:-500};   set_faults 0 "$ms" "order-api adds ${ms} ms of latency" ;;
  reset) set_faults 0 0 "order-api healthy again (no injected faults)" ;;
  watch) watch_rollout ;;
  *) echo "unknown command: $cmd"; exit 2 ;;
esac
