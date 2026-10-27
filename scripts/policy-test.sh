#!/usr/bin/env bash
# Tries to run images that must be refused in a signed-image namespace and shows Kyverno's answer.
set -uo pipefail
NS=${NS:-sentinel-staging}
OWNER=${OWNER:-sudhanshu5439}
pass=0; fail=0

try() { # name, image, expectation
  local name=$1 image=$2
  echo "==> $name"
  echo "    image: $image"
  out=$(kubectl -n "$NS" run "policy-test-$RANDOM" --image="$image" --restart=Never \
        --overrides='{"spec":{"containers":[{"name":"t","image":"'"$image"'","resources":{"requests":{"cpu":"10m","memory":"16Mi"},"limits":{"memory":"32Mi"}},"readinessProbe":{"exec":{"command":["true"]}},"securityContext":{"allowPrivilegeEscalation":false,"runAsNonRoot":true,"capabilities":{"drop":["ALL"]},"seccompProfile":{"type":"RuntimeDefault"}}}]}}' \
        --dry-run=server 2>&1)
  if echo "$out" | grep -qiE "denied|forbidden|violat"; then
    echo "    REFUSED (expected): $(echo "$out" | tr '\n' ' ' | cut -c1-220)"; pass=$((pass+1))
  else
    echo "    ALLOWED (unexpected): $out"; fail=$((fail+1))
  fi
}

try "Image from Docker Hub"                  "nginx:1.27"
try "Sentinel image by tag, not digest"      "ghcr.io/${OWNER}/sentinel-order-api:main"
try "Image built on the laptop (unsigned)"   "sentinel-order-api:dev"

echo; echo "refused: $pass   unexpectedly allowed: $fail"
[ "$fail" -eq 0 ]
