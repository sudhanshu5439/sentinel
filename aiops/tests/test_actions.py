"""The action policy and its guards: what the assistant may do, and what stops it.

These tests are the specification of Step 8. The model only ever influences the *category*;
the target always comes from cluster facts in the pack, and every path ends in the catalogue.
"""

from datetime import timedelta

import pytest
from conftest import T0

from assistant.actions import (
    ActionKind,
    ActionStatus,
    Guards,
    Tier,
    apply_guards,
    decide,
)
from assistant.diagnosis import (
    Category,
    Claim,
    Confidence,
    Diagnosis,
    DiagnosisResult,
    DiagnosisStatus,
)
from assistant.evidence import Evidence, EvidenceKind
from assistant.pack import build_pack

STAGING, PROD = "sentinel-staging", "sentinel-prod"
PRODS = frozenset({PROD})
REVISION = "3f2c1a9d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39"


def change(title: str, **data) -> Evidence:
    return Evidence(
        kind=EvidenceKind.CHANGE,
        source="kubernetes",
        title=title,
        detail="",
        at=T0 - timedelta(minutes=3),
        data={"suspect_score": 0.9, "reasons": [], **data},
    )


def workload(owner="order-api", live=True, **extra) -> Evidence:
    return change(
        f"new revision of Rollout/{owner}",
        change="workload",
        owner=owner,
        sync_app="sentinel-staging",
        sync_revision=REVISION,
        live=live,
        **extra,
    )


def rollout(phase: str, canary="aaa", stable="bbb") -> Evidence:
    return change(
        f"rollout order-api is {phase}",
        change="rollout",
        rollout="order-api",
        phase=phase,
        canary_hash=canary,
        stable_hash=stable,
    )


def diagnosed(category=Category.BAD_RELEASE, confidence=Confidence.HIGH, cites=("E1",), **kw):
    return DiagnosisResult(
        status=DiagnosisStatus.DIAGNOSED,
        diagnosis=Diagnosis(
            summary="s",
            category=category,
            confidence=confidence,
            probable_cause=Claim(statement="the new release", evidence=list(cites)),
            supporting=[],
            missing_evidence=[],
            injection_suspected=kw.get("injection", False),
        ),
    )


def guards(per_hour=3, cooldown=30) -> Guards:
    return Guards(frozenset({STAGING, PROD}), per_hour, timedelta(minutes=cooldown))


# --- the policy --------------------------------------------------------------------------------


def test_bad_release_in_staging_at_high_confidence_reverts_the_causing_commit_now():
    pack = build_pack([workload()])
    d = decide(diagnosed(), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.REVERT_COMMIT and d.tier is Tier.ACT
    # The target is the Git commit recorded by the correlator, not anything the model wrote.
    assert d.target == {"namespace": STAGING, "app": "sentinel-staging", "revision": REVISION}
    assert d.evidence == ["E1"]


def test_the_same_diagnosis_in_prod_needs_approval():
    pack = build_pack([workload()])
    d = decide(diagnosed(), pack, PROD, "order-api", PRODS)
    assert d.action is ActionKind.REVERT_COMMIT and d.tier is Tier.APPROVE
    assert any("production" in r for r in d.reasons)


def test_medium_confidence_needs_approval_even_in_staging():
    pack = build_pack([workload()])
    d = decide(diagnosed(confidence=Confidence.MEDIUM), pack, STAGING, "order-api", PRODS)
    assert d.tier is Tier.APPROVE


def test_a_canary_in_flight_is_aborted_rather_than_reverted():
    pack = build_pack([workload(), rollout("Paused")])
    d = decide(diagnosed(), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.ABORT_CANARY and d.tier is Tier.ACT
    assert d.target == {"namespace": STAGING, "rollout": "order-api"}


@pytest.mark.parametrize("phase", ["Healthy", "Degraded"])
def test_a_finished_or_already_aborted_rollout_is_not_aborted_again(phase):
    pack = build_pack([workload(), rollout(phase)])
    assert decide(diagnosed(), pack, STAGING, "order-api", PRODS).action is ActionKind.REVERT_COMMIT


@pytest.mark.parametrize("category", [Category.DEPENDENCY, Category.RESOURCE, Category.UNKNOWN])
def test_causes_outside_our_own_changes_only_notify(category):
    """Scenarios 4 and 5: a database or network fault must never trigger a rollback."""
    pack = build_pack([workload()])
    d = decide(diagnosed(category=category), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.NOTIFY and d.tier is Tier.NOTIFY


def test_low_confidence_only_notifies():
    pack = build_pack([workload()])
    d = decide(diagnosed(confidence=Confidence.LOW), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.NOTIFY


@pytest.mark.parametrize("status", [DiagnosisStatus.ESCALATE, DiagnosisStatus.SKIPPED])
def test_no_validated_diagnosis_means_no_action(status):
    d = decide(
        DiagnosisResult(status=status), build_pack([workload()]), STAGING, "order-api", PRODS
    )
    assert d.action is ActionKind.NOTIFY
    assert decide(None, None, STAGING, "order-api", PRODS).action is ActionKind.NOTIFY


def test_traffic_proposes_a_scale_out_that_always_needs_approval():
    """Scenario 7: capacity, not a fault."""
    d = decide(diagnosed(category=Category.TRAFFIC), build_pack([]), STAGING, "order-api", PRODS)
    assert d.action is ActionKind.SCALE_OUT and d.tier is Tier.APPROVE


def test_suspected_injection_caps_the_tier_at_approval():
    """Scenario 9: the action stays the same, but a human looks first."""
    pack = build_pack([workload(), rollout("Paused")])
    d = decide(diagnosed(injection=True), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.ABORT_CANARY and d.tier is Tier.APPROVE


def test_a_change_to_another_service_is_not_reverted():
    pack = build_pack([workload(owner="risk-engine")])
    d = decide(diagnosed(), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.NOTIFY
    assert "not a change to order-api" in d.reasons[-1]


@pytest.mark.parametrize("live", [False, None])
def test_a_change_that_is_no_longer_running_is_not_reverted(live):
    """8b live run: a DB outage 13 min after an aborted canary got a revert proposal for the
    canary's commit. A revision scaled to 0, or one whose state is unknown, is not undone."""
    d = decide(diagnosed(), build_pack([workload(live=live)]), STAGING, "order-api", PRODS)
    assert d.action is ActionKind.NOTIFY
    assert "no longer running" in d.reasons[-1]


def test_a_cited_sync_is_reverted_only_if_what_it_produced_still_runs():
    sync = change("Argo CD synced", change="sync", app="sentinel-staging", revision=REVISION)
    for live, expected in ((True, ActionKind.REVERT_COMMIT), (False, ActionKind.NOTIFY)):
        pack = build_pack([sync, workload(live=live)])
        (sync_id,) = [i.id for i in pack.items if i.facts.get("change") == "sync"]
        d = decide(diagnosed(cites=(sync_id,)), pack, STAGING, "order-api", PRODS)
        assert d.action is expected


def test_a_change_with_no_linked_commit_is_not_reverted():
    e = workload()
    del e.data["sync_revision"], e.data["sync_app"]
    d = decide(diagnosed(), build_pack([e]), STAGING, "order-api", PRODS)
    assert d.action is ActionKind.NOTIFY


def test_the_model_cannot_point_the_action_at_an_item_it_did_not_cite_as_the_cause():
    """Only probable_cause citations count: a revision is never read from a supporting claim or
    from text, so an injected 'revert commit deadbeef' has nothing to attach to."""
    pack = build_pack([workload(), change("noise", change="none", summary=True)])
    (noise,) = [i.id for i in pack.items if i.facts.get("change") == "none"]
    d = decide(diagnosed(cites=(noise,)), pack, STAGING, "order-api", PRODS)
    assert d.action is ActionKind.NOTIFY


# --- the guards --------------------------------------------------------------------------------


def revert(namespace=STAGING):
    return decide(diagnosed(), build_pack([workload()]), namespace, "order-api", PRODS)


def test_dry_run_is_the_default_outcome_and_changes_nothing():
    d = apply_guards(revert(), guards(), STAGING, "order-api", True, dry_run=True, now=T0)
    assert d.status is ActionStatus.DRY_RUN and d.blocked_by is None


def test_live_tier_1_is_ready_and_live_tier_2_waits_for_a_human():
    g = guards()
    assert apply_guards(revert(), g, STAGING, "order-api", True, False, T0).status is (
        ActionStatus.READY
    )
    assert apply_guards(revert(PROD), g, PROD, "order-api", True, False, T0).status is (
        ActionStatus.AWAITING_APPROVAL
    )


def test_the_kill_switch_blocks_actions_but_not_notifications():
    g = guards()
    d = apply_guards(revert(), g, STAGING, "order-api", False, False, T0)
    assert d.status is ActionStatus.BLOCKED and "kill switch" in d.blocked_by
    notify = decide(None, None, STAGING, "order-api", PRODS)
    assert apply_guards(notify, g, STAGING, "order-api", False, False, T0).status is (
        ActionStatus.NOTIFY_ONLY
    )


def test_blast_radius_rejects_namespaces_outside_the_allow_list():
    d = revert("kube-system")
    d = apply_guards(d, guards(), "kube-system", "order-api", True, False, T0)
    assert d.status is ActionStatus.BLOCKED and "blast radius" in d.blocked_by


def test_one_action_per_service_per_cooldown():
    g = guards(cooldown=30)
    assert apply_guards(revert(), g, STAGING, "order-api", True, False, T0).status is (
        ActionStatus.READY
    )
    again = apply_guards(revert(), g, STAGING, "order-api", True, False, T0 + timedelta(minutes=5))
    assert again.status is ActionStatus.BLOCKED and "had an action 5 min ago" in again.blocked_by
    later = apply_guards(revert(), g, STAGING, "order-api", True, False, T0 + timedelta(minutes=31))
    assert later.status is ActionStatus.READY


def test_a_global_hourly_cap_across_services():
    g = guards(per_hour=2, cooldown=1)
    for minute in (0, 2):
        apply_guards(revert(), g, STAGING, "order-api", True, False, T0 + timedelta(minutes=minute))
    capped = apply_guards(revert(), g, STAGING, "order-api", True, False, T0 + timedelta(minutes=4))
    assert (
        capped.status is ActionStatus.BLOCKED and "2 actions in the last hour" in capped.blocked_by
    )


def test_dry_runs_count_against_the_rate_limit_so_dry_run_predicts_live():
    g = guards()
    apply_guards(revert(), g, STAGING, "order-api", True, True, T0)
    second = apply_guards(revert(), g, STAGING, "order-api", True, True, T0 + timedelta(minutes=1))
    assert second.status is ActionStatus.BLOCKED


# --- audit: no audit, no action -----------------------------------------------------------------


def incident_with_release(clock):
    from conftest import make_alert

    from assistant.grouping import IncidentGrouper

    grouper = IncidentGrouper(window=timedelta(minutes=15), clock=clock)
    incident = grouper.ingest(make_alert(namespace=STAGING)).incident
    incident.pack = build_pack([workload()])
    incident.diagnosis = diagnosed()
    incident.diagnosis.prompt_sha256 = "ab" * 32
    return incident


def run_decide(incident, audit, live=True):
    import asyncio
    from dataclasses import replace

    from test_api import SETTINGS

    from assistant.main import decide_and_audit

    settings = replace(SETTINGS, action_mode_live=live, actions_switch_file="/nonexistent")
    asyncio.run(decide_and_audit(incident, settings, guards(), audit))


def test_every_decision_is_audited_with_the_evidence_and_prompt_hashes(clock):
    from assistant.audit import MemoryAudit

    audit, incident = MemoryAudit(), incident_with_release(clock)
    run_decide(incident, audit)
    diagnosis, decision = audit.rows
    assert diagnosis["event"] == "diagnosis" and decision["event"] == "decision"
    assert diagnosis["prompt_sha256"] == "ab" * 32 and len(diagnosis["pack_sha256"]) == 64
    assert diagnosis["payload"]["pack"]["items"][0]["facts"]["sync_revision"] == REVISION
    assert decision["action"] == "revert_commit" and decision["status"] == "ready"
    assert incident.audited is True and incident.decision.status is ActionStatus.READY


def test_a_live_action_is_blocked_when_the_audit_log_cannot_be_written(clock):
    from assistant.audit import MemoryAudit

    incident = incident_with_release(clock)
    run_decide(incident, MemoryAudit(fail=True))
    assert incident.decision.status is ActionStatus.BLOCKED
    assert "no audit, no action" in incident.decision.blocked_by
    assert incident.audited is False


def test_without_an_audit_log_live_actions_are_blocked_but_dry_runs_still_record(clock):
    incident = incident_with_release(clock)
    run_decide(incident, None, live=True)
    assert incident.decision.status is ActionStatus.BLOCKED
    incident = incident_with_release(clock)
    run_decide(incident, None, live=False)
    assert incident.decision.status is ActionStatus.DRY_RUN
