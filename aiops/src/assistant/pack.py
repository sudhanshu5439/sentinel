"""Evidence pack: raw evidence → a small, ranked, redacted list of items the LLM can cite (7c).

Order of operations matters:
1. redact     secrets and personal data never leave the cluster, whatever happens next
2. collapse   repeated log lines and events become one template with a count
3. rank       gaps first (the model must know what it could not see), then changes, then the
              most abnormal metrics, events and log templates
4. budget     add items in rank order until the token budget is spent; count what was dropped
5. number     E1, E2, ... in the final order, so every claim in the diagnosis can cite an ID

Each item's text is also capped: telemetry is attacker-writable, so the less free text reaches
the model, the smaller the prompt-injection surface.
"""

import json
import re
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel

from .evidence import Evidence, EvidenceKind

DEFAULT_TOKEN_BUDGET = 6000
MAX_TEXT_CHARS = 300

# --- 1. redaction -------------------------------------------------------------------------------

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1[REDACTED]"),
    # user:password@ in connection strings (postgres://app:s3cret@db:5432)
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (
        re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\"?\s*[=:]\s*\"?)[^\s\",}]+"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def clean(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Redact, drop control characters, collapse whitespace, cap the length."""
    text = " ".join(_CONTROL.sub(" ", redact(text)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- 2. templating ------------------------------------------------------------------------------

_VARIABLE_PARTS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<uuid>",
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"(?<![A-Za-z<])-?\d+(?:\.\d+)?"), "<n>"),
]


def log_message(line: str) -> str:
    """The human part of a log line: `msg` (+ `error`) for our JSON logs, else the raw line."""
    try:
        entry = json.loads(line)
    except ValueError:
        return line
    if not isinstance(entry, dict):
        return line
    parts = [str(entry.get(k)) for k in ("msg", "message", "error") if entry.get(k)]
    return " | ".join(parts) or line


def template(text: str) -> str:
    for pattern, placeholder in _VARIABLE_PARTS:
        text = pattern.sub(placeholder, text)
    return text


# --- the pack -----------------------------------------------------------------------------------


class PackItem(BaseModel):
    id: str  # E1, E2, ...
    kind: EvidenceKind
    source: str
    title: str
    text: str
    count: int = 1
    first_at: datetime | None = None
    last_at: datetime | None = None

    def render(self) -> str:
        when = ""
        if self.first_at and self.last_at and self.first_at != self.last_at:
            when = f" {self.first_at:%H:%M:%S}–{self.last_at:%H:%M:%S}"
        elif self.last_at:
            when = f" {self.last_at:%H:%M:%S}"
        times = f" ×{self.count}" if self.count > 1 else ""
        return f"[{self.id}] {self.kind}/{self.source}{times}{when} | {self.title} | {self.text}"


class EvidencePack(BaseModel):
    items: list[PackItem]
    token_budget: int
    tokens_estimated: int
    dropped: dict[str, int] = {}  # kind -> items left out by the budget
    raw_items: int

    def ids(self) -> set[str]:
        return {i.id for i in self.items}

    def render(self) -> str:
        lines = [i.render() for i in self.items]
        if self.dropped:
            left = ", ".join(f"{n} {k}" for k, n in sorted(self.dropped.items()))
            lines.append(f"(not shown, over budget: {left})")
        return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    # ~4 characters per token for English/log text; good enough to keep a budget, not to bill.
    return len(text) // 4 + 1


class _Draft(BaseModel):
    rank: tuple[float, ...]  # (tier, -importance, ...): lower sorts first
    kind: EvidenceKind
    source: str
    title: str
    text: str
    count: int = 1
    first_at: datetime | None = None
    last_at: datetime | None = None


def _metric_importance(e: Evidence) -> float:
    """How abnormal a metric looks: relative change against its baseline. State metrics
    (restarts, terminations, unready pods) have no baseline and count as clearly abnormal."""
    now, base = e.data.get("now"), e.data.get("baseline")
    if now is None:
        return 10.0
    try:
        now_f = float(now)
        base_f = float(base) if base not in (None, "NaN") else None
    except (TypeError, ValueError):
        return 0.0
    if base_f is None:
        return 1.0
    return abs(now_f - base_f) / max(abs(base_f), 1e-3)


def _grouped(evidence: list[Evidence], kind: EvidenceKind, key_text) -> list[_Draft]:
    groups: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for e in evidence:
        if e.kind is kind:
            groups[
                (
                    e.title if kind is EvidenceKind.LOG else e.title.split(" on ")[0],
                    template(key_text(e)),
                )
            ].append(e)
    drafts = []
    for (title, _), members in groups.items():
        times = sorted(m.at for m in members if m.at)
        count = sum(int(m.data.get("count", 1)) for m in members)
        example = clean(key_text(members[-1]))
        if kind is EvidenceKind.EVENT:
            objects = sorted({m.title.split(" on ", 1)[-1] for m in members})
            more = f" (+{len(objects) - 3} more)" if len(objects) > 3 else ""
            title = f"{title} on {', '.join(objects[:3])}{more}"
        tier = 3 if kind is EvidenceKind.EVENT else 4
        drafts.append(
            _Draft(
                rank=(tier, -count),
                kind=kind,
                source=members[0].source,
                title=clean(title, 160),
                text=example,
                count=count,
                first_at=times[0] if times else None,
                last_at=times[-1] if times else None,
            )
        )
    return drafts


def build_pack(evidence: list[Evidence], token_budget: int = DEFAULT_TOKEN_BUDGET) -> EvidencePack:
    drafts: list[_Draft] = []
    for e in evidence:
        if e.kind is EvidenceKind.GAP:
            drafts.append(
                _Draft(
                    rank=(0, 0),
                    kind=e.kind,
                    source=e.source,
                    title=clean(e.title, 160),
                    text=clean(e.detail),
                    last_at=e.at,
                )
            )
        elif e.kind is EvidenceKind.CHANGE:
            # Highest suspect score first (correlator, 7d); the "nothing changed" note leads, as
            # it is the key fact when present; ties broken by recency.
            score = e.data.get("suspect_score")
            order = 2.0 if e.data.get("summary") else float(score or 0.0)
            recency = -(e.at.timestamp()) if e.at else 0.0
            text = e.detail
            if score is not None and not e.data.get("summary"):
                text = f"suspect {score:.2f} ({'; '.join(e.data.get('reasons', []))}). {e.detail}"
            drafts.append(
                _Draft(
                    rank=(1, -order, recency),
                    kind=e.kind,
                    source=e.source,
                    title=clean(e.title, 160),
                    text=clean(text),
                    last_at=e.at,
                )
            )
        elif e.kind is EvidenceKind.METRIC:
            drafts.append(
                _Draft(
                    rank=(2, -_metric_importance(e)),
                    kind=e.kind,
                    source=e.source,
                    title=clean(e.title, 160),
                    text=clean(e.detail),
                    last_at=e.at,
                )
            )
    drafts += _grouped(evidence, EvidenceKind.EVENT, lambda e: e.detail)
    drafts += _grouped(evidence, EvidenceKind.LOG, lambda e: log_message(e.detail))
    drafts.sort(key=lambda d: d.rank)

    items: list[PackItem] = []
    dropped: dict[str, int] = defaultdict(int)
    used = 0
    for d in drafts:
        item = PackItem(id=f"E{len(items) + 1}", **d.model_dump(exclude={"rank"}))
        cost = estimate_tokens(item.render())
        # Gaps are never dropped: "I could not see the logs" must always reach the model.
        if used + cost > token_budget and d.kind is not EvidenceKind.GAP:
            dropped[str(d.kind)] += 1
            continue
        items.append(item)
        used += cost
    return EvidencePack(
        items=items,
        token_budget=token_budget,
        tokens_estimated=used,
        dropped=dict(dropped),
        raw_items=len(evidence),
    )
