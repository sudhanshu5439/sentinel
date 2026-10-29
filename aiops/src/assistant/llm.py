"""The one place that talks to an LLM. Everything else depends on the `LLM` protocol, so tests
use a fake and a provider change stays in this file.

Guardrails that belong to the transport live here: a hard timeout, a single SDK retry, a
max-token cap per call and JSON-schema-constrained output. The per-incident dollar budget is
enforced by the caller (diagnosis.py), which knows how many attempts an incident has had.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

# USD per million tokens (input, output). A model missing here cannot be budgeted, so it is
# refused rather than called blind.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}


class LLMError(Exception):
    """The call failed (timeout, network, API error). Never retried by the caller's loop."""


@dataclass(frozen=True)
class LLMReply:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    request_id: str | None = None


class LLM(Protocol):
    model: str

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> LLMReply: ...

    async def aclose(self) -> None: ...


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


class AnthropicLLM:
    """Claude through the official SDK. The API key comes from ANTHROPIC_API_KEY in the
    environment (a Kubernetes Secret in-cluster); it is never passed around or logged."""

    def __init__(self, model: str, timeout_seconds: float) -> None:
        if model not in PRICES:
            raise ValueError(f"no price known for model {model!r}: cannot enforce a budget")
        self.model = model
        # One retry for a dropped connection or 429/5xx; more would multiply cost and latency.
        self._client = anthropic.AsyncAnthropic(timeout=timeout_seconds, max_retries=1)

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> LLMReply:
        try:
            msg = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                # Constrained decoding: the reply is JSON matching the schema. Our own
                # validation still runs, because "valid JSON" is not "true and cited".
                # Low effort: this is triage over a small pack, not open-ended reasoning.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        text = "".join(b.text for b in msg.content if b.type == "text")
        return LLMReply(
            text=text,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            stop_reason=msg.stop_reason,
            request_id=msg._request_id,
        )

    async def aclose(self) -> None:
        await self._client.close()
