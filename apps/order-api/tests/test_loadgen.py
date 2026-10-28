import random

from order_api.loadgen import make_order
from order_api.models import OrderRequest


def test_order_mix_is_mostly_valid():
    rng = random.Random(42)
    orders = [make_order(rng) for _ in range(2000)]
    invalid = [o for o in orders if o["side"] == "HOLD"]
    oversized = [o for o in orders if o["quantity"] == 5000]
    assert 0.03 < len(invalid) / len(orders) < 0.07
    assert 0.12 < len(oversized) / len(orders) < 0.18
    for o in orders:
        if o["side"] != "HOLD":
            OrderRequest(**o)  # every non-invalid order passes API validation


def test_every_request_opens_a_new_connection():
    # A Service balances per TCP connection, not per request. A keep-alive connection would
    # pin all synthetic traffic to one pod, so a canary pod could receive nothing at all.
    from order_api.loadgen import make_client

    with make_client("http://order-api:8000") as client:
        assert client.headers["connection"] == "close"
