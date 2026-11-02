"""After an action (8e): did it work? An action is a hypothesis about the cause; the SLO tells.

The service's 5xx ratio (the SLI the burn-rate alerts are built on) is read every
`poll_seconds`. Two healthy readings in a row = recovered. Still failing after `timeout_seconds`
= the action did not fix it: escalate to a human with the evidence. Either way it is audited.
A missing reading (no traffic, Prometheus down) is neither healthy nor failing; it is reported.
"""

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

log = logging.getLogger("assistant.verify")

ERROR_RATIO = (
    '(sum(rate(http_requests_total{{namespace="{ns}",app="{app}",status="5xx"}}[1m])) or vector(0))'
    ' / sum(rate(http_requests_total{{namespace="{ns}",app="{app}"}}[1m]))'
)


class Verification(BaseModel):
    recovered: bool | None  # None: could not tell (no data)
    readings: list[float | None]
    checked_for_seconds: float
    threshold: float
    finished_at: datetime

    def describe(self) -> str:
        last = next((r for r in reversed(self.readings) if r is not None), None)
        shown = f"{last:.2%}" if last is not None else "no data"
        if self.recovered:
            return f"recovered: 5xx ratio {shown} (below {self.threshold:.0%}, twice in a row)"
        if self.recovered is False:
            return (
                f"NOT recovered after {int(self.checked_for_seconds // 60)} min: 5xx ratio "
                f"{shown}; the action did not fix it, a human takes over"
            )
        return f"could not verify: no 5xx data for {int(self.checked_for_seconds // 60)} min"


class Verifier:
    def __init__(self, http: httpx.AsyncClient, prometheus_url: str) -> None:
        self._http, self._url = http, prometheus_url.rstrip("/")

    async def ratio(self, namespace: str, app: str) -> float | None:
        r = await self._http.get(
            f"{self._url}/api/v1/query", params={"query": ERROR_RATIO.format(ns=namespace, app=app)}
        )
        r.raise_for_status()
        result = r.json()["data"]["result"]
        if not result or result[0]["value"][1] == "NaN":
            return None
        return float(result[0]["value"][1])

    async def watch(
        self,
        namespace: str,
        app: str,
        poll_seconds: float,
        timeout_seconds: float,
        threshold: float = 0.01,
    ) -> Verification:
        readings: list[float | None] = []
        waited, healthy_in_a_row = 0.0, 0
        while waited < timeout_seconds:
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            try:
                value = await self.ratio(namespace, app)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("verify %s/%s: query failed: %r", namespace, app, exc)
                value = None
            readings.append(value)
            healthy_in_a_row = (
                healthy_in_a_row + 1 if value is not None and value < threshold else 0
            )
            if healthy_in_a_row >= 2:
                return Verification(
                    recovered=True,
                    readings=readings,
                    checked_for_seconds=waited,
                    threshold=threshold,
                    finished_at=datetime.now(UTC),
                )
        seen = [r for r in readings if r is not None]
        return Verification(
            recovered=False if seen else None,
            readings=readings,
            checked_for_seconds=waited,
            threshold=threshold,
            finished_at=datetime.now(UTC),
        )
