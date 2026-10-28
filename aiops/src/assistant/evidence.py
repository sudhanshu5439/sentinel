"""Evidence: the facts collected about an incident, before trimming (7c) gives each an ID."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, field_validator

# A Kubernetes namespace name (RFC 1123 label). It is pasted into PromQL/LogQL/URL paths, and it
# comes from alert labels, so anything else is rejected rather than escaped.
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class EvidenceKind(StrEnum):
    METRIC = "metric"  # a number now vs. a baseline (error ratio, p95, restarts)
    LOG = "log"  # a warning/error log line
    EVENT = "event"  # a Kubernetes Warning event
    CHANGE = "change"  # something changed: Argo CD sync, rollout step
    GAP = "gap"  # a collector failed: the diagnosis must know what it could NOT see


class Evidence(BaseModel):
    kind: EvidenceKind
    source: str  # prometheus | loki | kubernetes | argocd | rollouts
    title: str  # one line, e.g. "order-api 5xx ratio (5m)"
    detail: str = ""
    at: datetime | None = None
    data: dict[str, Any] = {}


class Scope(BaseModel):
    """Where and when to look: the incident's namespace, from `start` (before it opened) to now."""

    namespace: str
    service: str
    start: datetime
    end: datetime

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        if not _NAMESPACE.match(value):
            raise ValueError(f"not a valid namespace name: {value!r}")
        return value
