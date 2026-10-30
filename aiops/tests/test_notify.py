import asyncio
import json

import httpx
from conftest import T0
from test_diagnosis import answer, pack

from assistant.diagnosis import DiagnosisResult, DiagnosisStatus, validate
from assistant.models import AlertRecord, Incident, IncidentStatus
from assistant.notify import SlackNotifier, build_message, slack_escape


def incident(result: DiagnosisResult | None) -> Incident:
    return Incident(
        id="20261030T100000Z-staging-order-api",
        namespace="staging",
        service="order-api",
        status=IncidentStatus.OPEN,
        opened_at=T0,
        last_activity_at=T0,
        alerts={
            "f1": AlertRecord(
                fingerprint="f1",
                alertname="OrderApiAvailabilityBudgetBurnFast",
                severity="critical",
                status="firing",
                starts_at=T0,
                labels={},
            )
        },
        pack=pack(),
        diagnosis=result,
    )


def diagnosed(**overrides) -> DiagnosisResult:
    d, errors = validate(answer(**overrides), pack())
    assert not errors
    return DiagnosisResult(
        status=DiagnosisStatus.DIAGNOSED,
        model="claude-haiku-5-5",
        diagnosis=d,
        attempts=1,
        cost_usd=0.0006,
    )


def text_of(message: dict) -> str:
    return json.dumps(message)


def test_diagnosed_message_shows_cause_with_its_cited_evidence():
    msg = build_message(incident(diagnosed()))
    blocks = text_of(msg)
    assert "config_change (high confidence)" in msg["text"]
    assert "`E1`" in blocks and "faults-a" in blocks  # the cited change, quoted
    assert "$0.0006" in blocks and "no action taken" in blocks


def test_slack_control_syntax_from_model_or_telemetry_is_neutralised():
    s = slack_escape("<!channel> see <https://evil.example|docs> @here http://x.io")
    assert "<" not in s and ">" not in s
    assert "https://" not in s and "http://" not in s
    assert "@here" not in s


def test_injection_flag_is_shown_prominently():
    msg = build_message(incident(diagnosed(injection_suspected=True)))
    assert msg["text"].startswith(":warning:")
    assert "looks like instructions" in text_of(msg)


def test_escalation_says_a_human_is_needed_and_why():
    r = DiagnosisResult(
        status=DiagnosisStatus.ESCALATE,
        model="claude-haiku-5-5",
        attempts=2,
        errors=["attempt 2: probable_cause cites IDs not in the pack: E99"],
    )
    msg = build_message(incident(r))
    assert "needs a human" in msg["text"]
    assert "E99" in text_of(msg)


def test_skipped_diagnosis_still_reports_top_evidence():
    r = DiagnosisResult(status=DiagnosisStatus.SKIPPED, errors=["LLM disabled"])
    msg = build_message(incident(r))
    assert "no LLM diagnosis" in msg["text"] and "`E1`" in text_of(msg)


def test_every_section_respects_slack_limits():
    long = "x" * 5000
    msg = build_message(
        incident(diagnosed(summary=long, supporting=[{"statement": long, "evidence": ["E2"]}] * 6))
    )
    for b in msg["blocks"]:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= 3000


def send(handler) -> str | None:
    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        notifier = SlackNotifier("https://hooks.slack.test/services/T/B/secret", client=client)
        try:
            return await notifier.send(incident(diagnosed()))
        finally:
            await notifier.aclose()

    return asyncio.run(go())


def test_send_posts_json_and_reports_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    assert send(handler) is None
    assert seen["body"]["blocks"][0]["type"] == "header"


def test_slack_failure_is_returned_not_raised_and_url_not_leaked():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    error = send(down)
    assert error and "unreachable" in error and "secret" not in error
    assert "404" in send(lambda r: httpx.Response(404, text="no_service"))


def test_the_policy_decision_is_shown_with_its_tier_status_and_reasons():
    from assistant.actions import ActionKind, ActionStatus, Decision, Tier

    i = incident(diagnosed())
    i.decision = Decision(
        action=ActionKind.REVERT_COMMIT,
        tier=Tier.ACT,
        target={"namespace": "staging", "app": "sentinel-staging", "revision": "3f2c1a9"},
        reasons=["diagnosis config_change/high", "revert gitops commit 3f2c1a9 <!channel>"],
        status=ActionStatus.DRY_RUN,
    )
    blocks = text_of(build_message(i))
    assert "tier 1" in blocks and "revert_commit" in blocks and "dry run" in blocks
    assert "revision=3f2c1a9" in blocks and "<!channel>" not in blocks
    assert "no action taken" in blocks


def test_a_failed_audit_write_is_visible_in_the_message():
    i = incident(diagnosed())
    i.audited = False
    assert "audit log write FAILED" in text_of(build_message(i))
