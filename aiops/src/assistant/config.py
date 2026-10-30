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
    # Break-glass kill switch, re-read before every diagnosis: a file containing "false" stops
    # LLM calls immediately, no restart. In-cluster it is an optional ConfigMap that is
    # deliberately not in Git, so Argo CD self-heal cannot undo it (`make aiops-off`).
    kill_switch_file: str = "/etc/assistant/switch/LLM_ENABLED"
    # Step 8, guarded actions. dry-run unless ACTION_MODE=live is set on purpose: the policy
    # runs and is audited, nothing is changed. ACTIONS_ENABLED=false in the same ConfigMap stops
    # actions but keeps diagnosing (`make actions-off`).
    action_mode_live: bool = False
    actions_switch_file: str = "/etc/assistant/switch/ACTIONS_ENABLED"
    allowed_namespaces: frozenset[str] = frozenset({"sentinel-staging", "sentinel-prod"})
    prod_namespaces: frozenset[str] = frozenset({"sentinel-prod"})
    actions_per_hour: int = 3
    action_cooldown_minutes: int = 30
    # PostgreSQL DSN for the append-only audit log (a credential: from a Secret). Unset = no
    # durable audit, and therefore no live actions (no audit, no action).
    audit_database_url: str | None = None
    # Revert PRs (8d). A fine-grained token for this one repository (contents + pull requests);
    # unset = dry-run plans still work (public repo, read-only API), live reverts fail.
    gitops_repo: str = "sudhanshu5439/sentinel-gitops"
    github_token: str | None = None
    # Tier 2: how often to check whether a human merged (approved) or closed (rejected) the PR.
    approval_poll_seconds: float = 60.0
    approval_timeout_minutes: int = 120

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
            kill_switch_file=os.getenv("KILL_SWITCH_FILE", "/etc/assistant/switch/LLM_ENABLED"),
            action_mode_live=os.getenv("ACTION_MODE", "dry-run").lower() == "live",
            actions_switch_file=os.getenv(
                "ACTIONS_SWITCH_FILE", "/etc/assistant/switch/ACTIONS_ENABLED"
            ),
            allowed_namespaces=_names(
                os.getenv("ALLOWED_NAMESPACES", "sentinel-staging,sentinel-prod")
            ),
            prod_namespaces=_names(os.getenv("PROD_NAMESPACES", "sentinel-prod")),
            actions_per_hour=min(max(int(os.getenv("ACTIONS_PER_HOUR", "3")), 0), 20),
            action_cooldown_minutes=max(int(os.getenv("ACTION_COOLDOWN_MINUTES", "30")), 1),
            audit_database_url=os.getenv("AUDIT_DATABASE_URL") or None,
            gitops_repo=os.getenv("GITOPS_REPO", "sudhanshu5439/sentinel-gitops"),
            github_token=os.getenv("GITHUB_TOKEN") or None,
            approval_poll_seconds=max(float(os.getenv("APPROVAL_POLL_SECONDS", "60")), 5.0),
            approval_timeout_minutes=max(int(os.getenv("APPROVAL_TIMEOUT_MINUTES", "120")), 1),
        )

    def kill_switch_engaged(self) -> bool:
        return _switched_off(self.kill_switch_file)

    def actions_enabled(self) -> bool:
        return not _switched_off(self.actions_switch_file)


def _names(value: str) -> frozenset[str]:
    return frozenset(n.strip() for n in value.split(",") if n.strip())


def _switched_off(path: str) -> bool:
    try:
        with open(path) as f:
            return f.read().strip().lower() == "false"
    except OSError:  # no file = switch not set
        return False
