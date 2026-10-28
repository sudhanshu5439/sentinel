from datetime import UTC, datetime, timedelta

import pytest

from assistant.models import Alert

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


def make_alert(
    alertname: str = "OrderApiAvailabilityBudgetBurnFast",
    service: str = "order-api",
    namespace: str = "staging",
    status: str = "firing",
    fingerprint: str | None = None,
) -> Alert:
    """Shaped like the real Sloth-style rules in the monitoring namespace."""
    return Alert.model_validate(
        {
            "status": status,
            "labels": {
                "alertname": alertname,
                "service": service,
                "namespace": namespace,
                "severity": "critical",
                "slo": f"{service}-availability",
            },
            "annotations": {"summary": f"{service} is burning its error budget in {namespace}"},
            "startsAt": T0.isoformat(),
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": fingerprint or f"{namespace}-{service}-{alertname}",
        }
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
