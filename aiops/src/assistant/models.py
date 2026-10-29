"""Data shapes: the Alertmanager webhook payload (input) and the incident (what we build from it).

Alertmanager webhook format (version 4):
https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .diagnosis import DiagnosisResult
from .evidence import Evidence
from .pack import EvidencePack


class Alert(BaseModel):
    """One alert as Alertmanager sends it. Unknown fields are ignored."""

    model_config = ConfigDict(populate_by_name=True)

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    starts_at: datetime = Field(alias="startsAt")
    # Alertmanager sends 0001-01-01T00:00:00Z while an alert is still firing.
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: str = Field(default="", alias="generatorURL")
    # Stable hash of the label set: the same alert re-sent has the same fingerprint.
    fingerprint: str


class WebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: str = "4"
    status: Literal["firing", "resolved"]
    receiver: str = ""
    group_key: str = Field(default="", alias="groupKey")
    alerts: list[Alert]


class IncidentStatus(StrEnum):
    OPEN = "open"  # at least one alert still firing
    RESOLVED = "resolved"  # every alert in it has resolved


class AlertRecord(BaseModel):
    """What the incident remembers about each distinct alert (by fingerprint)."""

    fingerprint: str
    alertname: str
    severity: str
    status: Literal["firing", "resolved"]
    starts_at: datetime
    ends_at: datetime | None = None
    summary: str = ""
    labels: dict[str, str]


class Incident(BaseModel):
    id: str
    namespace: str
    service: str
    status: IncidentStatus
    opened_at: datetime
    last_activity_at: datetime
    resolved_at: datetime | None = None
    alerts: dict[str, AlertRecord] = {}
    # Every webhook delivery that touched this incident, duplicates included: shows storm size.
    notifications: int = 0
    # Collected once, when the incident opens (one storm, one investigation).
    evidence: list[Evidence] = []
    evidence_collected_at: datetime | None = None
    # What the LLM will see: redacted, collapsed, ranked, numbered E1..En, within a token budget.
    pack: EvidencePack | None = None
    # The LLM's cited diagnosis, or an explicit escalation / skip with the reasons (7e).
    diagnosis: DiagnosisResult | None = None
