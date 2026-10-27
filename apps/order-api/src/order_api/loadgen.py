"""Synthetic traffic for order-api, so canary analysis always has data to judge.

Runs as its own Deployment from the same signed order-api image:
    python -m order_api.loadgen
A canary on a quiet cluster gets no requests, and "no errors" then means "no evidence".
This sends a steady, realistic mix: mostly valid orders, some over the risk limit, a few invalid.
"""

import logging
import os
import random
import signal
import time
from pathlib import Path

import httpx

log = logging.getLogger("loadgen")
SYMBOLS = ["INFY", "TCS", "HDFCBANK", "RELIANCE", "ICICIBANK", "SBIN"]
READY_FILE = Path(os.getenv("READY_FILE", "/tmp/ready"))  # noqa: S108  (emptyDir in the pod)


def make_order(rng: random.Random) -> dict:
    """80% normal orders, 15% over the notional limit (rejected by risk), 5% invalid (422)."""
    roll = rng.random()
    order = {
        "account_id": f"acc-{rng.randint(1, 50)}",
        "symbol": rng.choice(SYMBOLS),
        "side": rng.choice(["BUY", "SELL"]),
        "quantity": rng.randint(1, 100),
        "price": round(rng.uniform(100, 3000), 2),
    }
    if roll < 0.05:
        order["side"] = "HOLD"
    elif roll < 0.20:
        order["quantity"], order["price"] = 5000, 4000.0
    return order


def run(target: str, rps: float, stop: list[bool], rng: random.Random) -> dict[str, int]:
    stats: dict[str, int] = {}
    interval = 1.0 / rps
    last_report = time.monotonic()
    with httpx.Client(base_url=target, timeout=5) as client:
        while not stop[0]:
            started = time.monotonic()
            try:
                code = str(client.post("/orders", json=make_order(rng)).status_code)
            except httpx.HTTPError:
                code = "error"
            stats[code] = stats.get(code, 0) + 1
            if time.monotonic() - last_report >= 30:
                log.info("last 30s: %s", dict(sorted(stats.items())))
                stats, last_report = {}, time.monotonic()
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
    return stats


def main() -> None:
    logging.basicConfig(level="INFO", format="%(message)s")
    target = os.getenv("TARGET_URL", "http://order-api:8000")
    rps = float(os.getenv("RPS", "5"))
    stop = [False]
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))
    READY_FILE.touch()
    log.info("sending %.1f orders/s to %s", rps, target)
    run(target, rps, stop, random.Random())  # noqa: S311 (test traffic, not crypto)


if __name__ == "__main__":
    main()
