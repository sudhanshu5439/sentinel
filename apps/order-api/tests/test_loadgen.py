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
