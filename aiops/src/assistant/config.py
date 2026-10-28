"""Configuration, read once from environment variables (12-factor style)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str
    service_version: str
    # Alerts for the same service arriving within this window of the incident's last activity
    # join that incident instead of opening a new one.
    group_window_minutes: int
    # Incidents kept in memory (oldest dropped first). Durable storage arrives with the audit log.
    max_incidents: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", "sentinel-assistant"),
            service_version=os.getenv("SERVICE_VERSION", "dev"),
            group_window_minutes=max(int(os.getenv("GROUP_WINDOW_MINUTES", "15")), 1),
            max_incidents=max(int(os.getenv("MAX_INCIDENTS", "500")), 1),
        )
