"""8e: after an action, the SLI decides whether it worked."""

import asyncio

import httpx

from assistant.verify import Verifier


def verifier(values):
    """A fake Prometheus answering the error-ratio query with the given values in turn."""
    queue = list(values)

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        assert 'namespace="sentinel-staging"' in q and 'app="order-api"' in q
        v = queue.pop(0)
        result = [] if v is None else [{"metric": {}, "value": [0, str(v)]}]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    return Verifier(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "http://prom")


def watch(values, polls=10):
    """At most `polls` readings (0.01 s apart, so the test does not really wait)."""
    return asyncio.run(
        verifier(values).watch(
            "sentinel-staging", "order-api", poll_seconds=0.01, timeout_seconds=polls * 0.01 - 0.001
        )
    )


def test_two_healthy_readings_in_a_row_mean_recovered():
    v = watch([0.3, 0.004, 0.0])
    assert v.recovered is True and v.readings == [0.3, 0.004, 0.0]
    assert v.describe().startswith("recovered")


def test_one_good_reading_between_bad_ones_is_not_recovery():
    v = watch([0.3, 0.0, 0.2, 0.25], polls=4)
    assert v.recovered is False
    assert "NOT recovered" in v.describe() and "a human takes over" in v.describe()


def test_no_data_is_reported_as_unknown_not_as_healthy():
    v = watch([None, None, None], polls=3)
    assert v.recovered is None and "could not verify" in v.describe()
