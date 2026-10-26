#!/usr/bin/env bash
# End-to-end smoke test: order-api -> risk-engine -> PostgreSQL -> Redis Stream -> notifier.
set -euo pipefail
NS=${NAMESPACE:-sentinel}
PORT=18000

kubectl -n "$NS" port-forward svc/order-api "$PORT:8000" >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
for _ in $(seq 1 20); do curl -fs "localhost:$PORT/healthz" >/dev/null && break; sleep 0.5; done

post() { curl -fsS -X POST "localhost:$PORT/orders" -H 'content-type: application/json' -d "$1"; echo; }

echo "==> an order within limits (expect APPROVED)"
post '{"account_id":"acc-1","symbol":"INFY","side":"BUY","quantity":10,"price":1500.5}'
echo "==> an order above the notional limit (expect REJECTED)"
post '{"account_id":"acc-1","symbol":"TCS","side":"SELL","quantity":5000,"price":4000}'
echo "==> invalid input (expect HTTP 422)"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "localhost:$PORT/orders" \
  -H 'content-type: application/json' -d '{"account_id":"acc-1","symbol":"INFY","side":"HOLD","quantity":1,"price":1}'

echo "==> latest orders from PostgreSQL"
curl -fsS "localhost:$PORT/orders?limit=2"; echo

sleep 2
echo "==> notifier received:"
kubectl -n "$NS" logs deploy/notifier --tail=20 | grep notification | tail -2 || {
  echo "no notification found yet"; exit 1; }
echo -e "\n\033[32mSmoke test passed.\033[0m"
