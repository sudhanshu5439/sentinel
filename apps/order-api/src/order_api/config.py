"""Configuration, read once from environment variables (12-factor style)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str
    service_version: str
    database_url: str | None
    redis_url: str | None
    risk_engine_url: str
    risk_timeout_seconds: float
    # Fault injection knobs used by the chaos scenarios (bad-release simulations).
    fault_error_rate: float
    fault_latency_ms: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", "order-api"),
            service_version=os.getenv("SERVICE_VERSION", "dev"),
            database_url=os.getenv("DATABASE_URL"),
            redis_url=os.getenv("REDIS_URL"),
            risk_engine_url=os.getenv("RISK_ENGINE_URL", "http://risk-engine:8080"),
            risk_timeout_seconds=float(os.getenv("RISK_TIMEOUT_SECONDS", "2.0")),
            fault_error_rate=min(max(float(os.getenv("FAULT_ERROR_RATE", "0")), 0.0), 1.0),
            fault_latency_ms=max(int(os.getenv("FAULT_LATENCY_MS", "0")), 0),
        )
