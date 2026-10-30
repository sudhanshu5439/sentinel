"""Slack notification (7f): one message per investigated incident.

The message carries model-written text and evidence copied from telemetry, so it is escaped
like any untrusted input headed for a renderer:
- Slack control syntax is neutralised: &, <, > are entity-escaped, which defuses <!channel>,
  <@U123> mentions and <https://evil|click here> links; bare URLs are defanged so Slack does not
  turn them into clickable links
- every field is length-capped, so the message stays within Slack's block limits
Sending is best-effort: a Slack outage is logged and recorded on the incident, never raised.
"""

import logging
import re

import httpx

from .diagnosis import Diagnosis, DiagnosisResult, DiagnosisStatus
from .models import Incident
from .pack import EvidencePack

log = logging.getLogger("assistant.notify")

SECTION_LIMIT = 2900  # Slack allows 3000 characters per section text
_URL = re.compile(r"(?i)\b(https?|ftp)://")
_MENTION = re.compile(r"(?i)@(channel|here|everyone)\b")


def slack_escape(text: str, limit: int = 500) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _URL.sub(r"\1[:]//", text)
    text = _MENTION.sub(r"@​\1", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cited(pack: EvidencePack | None, ids: list[str]) -> list[str]:
    if pack is None:
        return []
    by_id = {i.id: i for i in pack.items}
    lines = []
    for eid in ids:
        item = by_id.get(eid)
        if item:
            lines.append(
                f"`{eid}` {item.kind} · {slack_escape(item.title, 120)}: "
                f"{slack_escape(item.text, 220)}"
            )
    return lines


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_LIMIT]}}


def _diagnosed(d: Diagnosis, pack: EvidencePack | None) -> list[dict]:
    cause = d.probable_cause
    blocks = [
        _section(slack_escape(d.summary, 600)),
        _section(
            "*Probable cause*  "
            + " ".join(f"`{e}`" for e in cause.evidence)
            + "\n"
            + slack_escape(cause.statement, 400)
            + "\n"
            + "\n".join(f"> {line}" for line in _cited(pack, cause.evidence[:4]))
        ),
    ]
    if d.supporting:
        blocks.append(
            _section(
                "*Supporting*\n"
                + "\n".join(
                    f"• {slack_escape(c.statement, 300)}  " + " ".join(f"`{e}`" for e in c.evidence)
                    for c in d.supporting
                )
            )
        )
    if d.missing_evidence:
        blocks.append(
            _section(
                "*Not visible to the assistant*\n"
                + "\n".join(f"• {slack_escape(m, 200)}" for m in d.missing_evidence)
            )
        )
    return blocks


def build_message(incident: Incident) -> dict:
    r: DiagnosisResult | None = incident.diagnosis
    where = f"{incident.service} in {incident.namespace}"
    alerts = ", ".join(sorted({a.alertname for a in incident.alerts.values()}))
    if r is not None and r.status is DiagnosisStatus.DIAGNOSED and r.diagnosis is not None:
        d = r.diagnosis
        icon = ":warning:" if d.injection_suspected else ":mag:"
        headline = f"{icon} {where}: {d.category} ({d.confidence} confidence)"
        body = _diagnosed(d, incident.pack)
        if d.injection_suspected:
            body.insert(
                0,
                _section(
                    ":warning: *The evidence contains text that looks like instructions to the "
                    "assistant.* It was treated as a symptom and not followed."
                ),
            )
    elif r is not None and r.status is DiagnosisStatus.ESCALATE:
        headline = f":rotating_light: {where}: no reliable diagnosis, needs a human"
        body = [_section("*Why*\n" + "\n".join(f"• {slack_escape(e, 300)}" for e in r.errors[:5]))]
    else:
        headline = f":information_source: {where}: evidence collected, no LLM diagnosis"
        reason = "; ".join(r.errors) if r else "not diagnosed"
        top = incident.pack.items[:3] if incident.pack else []
        body = [
            _section(
                f"Reason: {slack_escape(reason, 200)}\n"
                + "\n".join(f"> {line}" for line in _cited(incident.pack, [i.id for i in top]))
            )
        ]

    footer = f"Alerts: {slack_escape(alerts, 200)} · incident `{incident.id}`"
    if r is not None and r.model:
        footer += (
            f" · {r.model}, {r.attempts} attempt{'s' if r.attempts != 1 else ''}, ${r.cost_usd:.4f}"
        )
    footer += " · read-only: no action taken"
    return {
        "text": headline,  # notification / fallback text
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": headline[:150], "emoji": True},
            },
            *body,
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:2000]}]},
        ],
    }


class SlackNotifier:
    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = webhook_url  # a credential: never logged
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def send(self, incident: Incident) -> str | None:
        """Post the incident; returns None on success, else a short error (also logged)."""
        try:
            r = await self._client.post(self._url, json=build_message(incident))
        except httpx.HTTPError as exc:
            error = f"Slack unreachable: {type(exc).__name__}"
        else:
            if r.status_code == 200:
                return None
            error = f"Slack answered {r.status_code}: {r.text[:100]}"
        log.warning("incident %s: notification failed: %s", incident.id, error)
        return error

    async def aclose(self) -> None:
        await self._client.aclose()
