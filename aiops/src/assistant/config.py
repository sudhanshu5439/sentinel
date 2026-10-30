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
    # Evidence sources. An unset URL means that collector is off (it shows up as a gap).
    prometheus_url: str | None
    loki_url: str | None
    # In-cluster: https://kubernetes.default.svc with the ServiceAccount token. Locally: the
    # address of `kubectl proxy` (http://127.0.0.1:8001), which needs no token.
    kube_api_url: str | None
    kube_token_file: str
    kube_ca_file: str
    argocd_namespace: str
    # How far before the incident opened to look for changes, logs and events.
    lookback_minutes: int
    collect_timeout_seconds: float
    # Upper bound for the evidence pack sent to the LLM (estimated tokens).
    evidence_token_budget: int
    # LLM diagnosis. Off when LLM_ENABLED=false (the kill switch) or when no API key is set.
    llm_enabled: bool = False
    llm_model: str = "claude-haiku-5-5"
    llm_timeout_seconds: float = 30.0
    # Per call; Haiku's thinking tokens count toward it, so it is not just the JSON's size.
    llm_max_tokens: int = 2000
    # Per incident, across the first attempt and the retry. Worst case is checked before sending.
    llm_budget_usd: float = 0.02
    # Slack incoming webhook (a credential: from a Secret, never logged). Unset = no Slack.
    slack_webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        sa = "/var/run/secrets/kubernetes.io/serviceaccount"
        return cls(
            service_name=os.getenv("SERVICE_NAME", "sentinel-assistant"),
            service_version=os.getenv("SERVICE_VERSION", "dev"),
            group_window_minutes=max(int(os.getenv("GROUP_WINDOW_MINUTES", "15")), 1),
            max_incidents=max(int(os.getenv("MAX_INCIDENTS", "500")), 1),
            prometheus_url=os.getenv("PROMETHEUS_URL"),
            loki_url=os.getenv("LOKI_URL"),
            kube_api_url=os.getenv("KUBE_API_URL"),
            kube_token_file=os.getenv("KUBE_TOKEN_FILE", f"{sa}/token"),
            kube_ca_file=os.getenv("KUBE_CA_FILE", f"{sa}/ca.crt"),
            argocd_namespace=os.getenv("ARGOCD_NAMESPACE", "argocd"),
            lookback_minutes=max(int(os.getenv("LOOKBACK_MINUTES", "30")), 5),
            collect_timeout_seconds=float(os.getenv("COLLECT_TIMEOUT_SECONDS", "10")),
            evidence_token_budget=max(int(os.getenv("EVIDENCE_TOKEN_BUDGET", "6000")), 500),
            llm_enabled=os.getenv("LLM_ENABLED", "true").lower() != "false"
            and bool(os.getenv("ANTHROPIC_API_KEY")),
            llm_model=os.getenv("LLM_MODEL", "claude-haiku-5-5"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
            llm_max_tokens=min(max(int(os.getenv("LLM_MAX_TOKENS", "2000")), 256), 8000),
            llm_budget_usd=min(float(os.getenv("LLM_BUDGET_USD", "0.02")), 1.0),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
        )
