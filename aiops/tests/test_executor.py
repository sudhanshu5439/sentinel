"""Executors against a fake GitHub and a fake Kubernetes API (httpx.MockTransport). Response
shapes follow the GitHub REST API (commits, compare, contents, git data, pulls)."""

import asyncio
import json

import httpx
import pytest

from assistant.actions import ActionKind, ActionStatus, Decision, Tier
from assistant.executor import Executor, GitOpsRepo, RolloutsApi, overlay_path

REV, PARENT, MAIN = "c" * 40, "p" * 40, "m" * 40
FILE = "apps/overlays/staging/kustomization.yaml"
BLOB_AT_REV, BLOB_AT_PARENT = "b" * 40, "a" * 40


class FakeGitHub:
    """Remembers every write, so tests can assert that dry-run writes nothing."""

    def __init__(self, files=None, main_blob=BLOB_AT_REV, merged_by=None, state="open"):
        default = [{"filename": FILE, "status": "modified", "sha": BLOB_AT_REV}]
        self.files = default if files is None else files
        self.main_blob, self.merged_by, self.state = main_blob, merged_by, state
        self.writes: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/repos/o/gitops")
        if request.method != "GET":
            self.writes.append((request.method, path, json.loads(request.content or b"{}")))
        ref = request.url.params.get("ref")
        routes = {
            ("GET", f"/commits/{REV}"): {"sha": REV, "parents": [{"sha": PARENT}]},
            ("GET", f"/compare/{PARENT}...{REV}"): {"files": self.files},
            ("GET", "/git/ref/heads/main"): {"object": {"sha": MAIN}},
            ("GET", f"/git/commits/{MAIN}"): {"tree": {"sha": "t" * 40}},
            ("POST", "/git/trees"): {"sha": "n" * 40},
            ("POST", "/git/commits"): {"sha": "r" * 40},
            ("POST", "/git/refs"): {"ref": "x"},
            ("POST", "/pulls"): {"number": 42, "html_url": "https://github.com/o/gitops/pull/42"},
            ("PUT", "/pulls/42/merge"): {"merged": True},
            ("GET", "/pulls/42"): {
                "state": self.state,
                "merged": self.merged_by is not None,
                "merged_by": {"login": self.merged_by} if self.merged_by else None,
            },
        }
        if request.method == "GET" and path.startswith("/contents/"):
            sha = self.main_blob if ref == "main" else BLOB_AT_PARENT
            return httpx.Response(200, json={"sha": sha})
        body = routes.get((request.method, path))
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)


def repo(fake: FakeGitHub, token: str | None = "t") -> GitOpsRepo:  # noqa: S107 - a fake
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return GitOpsRepo(http, "o/gitops", token, api="https://api.github.test")


def revert(tier=Tier.ACT, status=ActionStatus.READY) -> Decision:
    return Decision(
        action=ActionKind.REVERT_COMMIT,
        tier=tier,
        target={"namespace": "sentinel-staging", "app": "sentinel-staging", "revision": REV},
        reasons=["diagnosis bad_release/high", f"revert gitops commit {REV[:7]}"],
        status=status,
    )


def run(coro):
    return asyncio.run(coro)


def test_overlay_path_is_the_environment_folder():
    assert overlay_path("sentinel-staging") == "apps/overlays/staging/"
    assert overlay_path("sentinel-prod") == "apps/overlays/prod/"


def test_a_revert_restores_each_file_from_the_parent_commit():
    plan = run(repo(FakeGitHub()).plan_revert(REV, "sentinel-staging"))
    assert plan.refused is None
    assert plan.parent == PARENT and plan.files == [(FILE, BLOB_AT_PARENT)]


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (
            FakeGitHub(files=[{"filename": "platform/policies/x.yaml", "status": "modified"}]),
            "outside apps/overlays/staging/",
        ),
        (
            FakeGitHub(files=[{"filename": "apps/overlays/prod/k.yaml", "status": "modified"}]),
            "outside apps/overlays/staging/",
        ),
        (FakeGitHub(files=[{"filename": FILE, "status": "removed"}]), "was removed"),
        # A later commit changed the same file: the commit may already be undone (8b run 2).
        (FakeGitHub(main_blob="d" * 40), "has changed since"),
        (FakeGitHub(files=[]), "changes no files"),
    ],
)
def test_a_revert_that_is_not_a_clean_small_undo_is_refused(fake, reason):
    plan = run(repo(fake).plan_revert(REV, "sentinel-staging"))
    assert plan.refused and reason in plan.refused


def test_dry_run_computes_the_plan_and_writes_nothing():
    fake = FakeGitHub()
    d = revert()
    d.status = ActionStatus.DRY_RUN
    run(Executor(repo(fake), None).run(d, "inc-1", dry_run=True))
    assert fake.writes == []
    assert d.status is ActionStatus.DRY_RUN
    assert d.result.startswith("would open a PR: revert cccccc") and "merge it" in d.result
    assert d.target["plan"]["files"] == [(FILE, BLOB_AT_PARENT)]


def test_tier_1_live_opens_the_pr_from_parent_blobs_and_merges_it():
    fake = FakeGitHub()
    d = revert()
    run(Executor(repo(fake), None).run(d, "20261030T120000Z-sentinel-staging-order-api", False))
    methods = [(m, p) for m, p, _ in fake.writes]
    assert methods == [
        ("POST", "/git/trees"),
        ("POST", "/git/commits"),
        ("POST", "/git/refs"),
        ("POST", "/pulls"),
        ("PUT", "/pulls/42/merge"),
    ]
    tree = fake.writes[0][2]
    assert tree["tree"] == [{"path": FILE, "mode": "100644", "type": "blob", "sha": BLOB_AT_PARENT}]
    assert fake.writes[1][2]["parents"] == [MAIN]  # on top of today's main, not the old parent
    assert d.status is ActionStatus.EXECUTED and "merged PR #42" in d.result
    assert d.target["pr"] == 42


def test_tier_2_live_opens_the_pr_and_a_human_merge_is_the_approval():
    fake = FakeGitHub()
    d = revert(Tier.APPROVE, ActionStatus.AWAITING_APPROVAL)
    ex = Executor(repo(fake), None)
    run(ex.run(d, "inc-2", dry_run=False))
    assert ("PUT", "/pulls/42/merge") not in [(m, p) for m, p, _ in fake.writes]
    assert d.status is ActionStatus.AWAITING_APPROVAL and "waiting for a human" in d.result

    fake.merged_by = "sudhanshu5439"
    assert run(ex.await_approval(d, poll_seconds=0, timeout_seconds=1)) is True
    assert d.status is ActionStatus.APPROVED and d.approver == "sudhanshu5439"


def test_closing_the_pr_rejects_the_proposal():
    fake = FakeGitHub(state="closed")
    d = revert(Tier.APPROVE, ActionStatus.AWAITING_APPROVAL)
    d.target["pr"] = 42
    assert run(Executor(repo(fake), None).await_approval(d, 0, 1)) is True
    assert d.status is ActionStatus.REJECTED


def test_live_without_a_token_fails_loudly_instead_of_pretending():
    d = revert()
    run(Executor(repo(FakeGitHub(), token=None), None).run(d, "inc-3", dry_run=False))
    assert d.status is ActionStatus.FAILED and "no GitHub token" in d.result


def test_a_refused_live_revert_is_blocked_with_the_reason():
    d = revert()
    run(Executor(repo(FakeGitHub(main_blob="d" * 40)), None).run(d, "inc-4", dry_run=False))
    assert d.status is ActionStatus.BLOCKED and "has changed since" in d.blocked_by


def abort(status=ActionStatus.READY) -> Decision:
    return Decision(
        action=ActionKind.ABORT_CANARY,
        tier=Tier.ACT,
        target={"namespace": "sentinel-staging", "rollout": "order-api"},
        reasons=["canary in flight"],
        status=status,
    )


def test_abort_patches_only_the_rollout_status_subresource():
    seen = []

    def kube(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["content-type"]))
        assert json.loads(request.content) == {"status": {"abort": True}}
        return httpx.Response(200, json={})

    api = RolloutsApi(httpx.AsyncClient(transport=httpx.MockTransport(kube)), "https://k8s")
    d = abort()
    run(Executor(None, api).run(d, "inc-5", dry_run=False))
    assert seen == [
        (
            "PATCH",
            "/apis/argoproj.io/v1alpha1/namespaces/sentinel-staging/rollouts/order-api/status",
            "application/merge-patch+json",
        )
    ]
    assert d.status is ActionStatus.EXECUTED


def test_dry_run_abort_only_says_what_it_would_do():
    d = abort(ActionStatus.DRY_RUN)
    run(Executor(None, None).run(d, "inc-6", dry_run=True))
    assert d.result.startswith("would PATCH rollouts/order-api/status")


def test_a_kubernetes_error_marks_the_action_failed_and_never_raises():
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"reason": "Forbidden"})

    api = RolloutsApi(httpx.AsyncClient(transport=httpx.MockTransport(forbidden)), "https://k8s")
    d = abort()
    run(Executor(None, api).run(d, "inc-7", dry_run=False))
    assert d.status is ActionStatus.FAILED and "403" in d.result
