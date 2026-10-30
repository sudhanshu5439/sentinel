"""Executors (8c/8d): carry out a decision the policy made. Nothing here chooses anything.

abort_canary   PATCH the Rollout's *status* subresource with {"status": {"abort": true}}, what
               `kubectl argo rollouts abort` does. RBAC: `patch` on `rollouts/status` only, for
               named rollouts, in staging and prod: it cannot change the spec, image or replicas.
revert_commit  A pull request in sentinel-gitops that restores, file by file, what the causing
               commit changed. Built with the Git Data API from the parent commit's blobs, so no
               file content is generated or uploaded. Refused (never "best effort") when:
                 - the commit touches anything outside that environment's overlay (blast radius)
                 - it adds, deletes or renames files (a revert that is no longer a small edit)
                 - a later commit changed one of the same files (the commit may already be undone,
                   as in the 8b run 2 trap; a human resolves it)
               Tier 1 merges the PR; tier 2 leaves it open: a human merging it is the approval,
               and GitHub records who did. Argo CD then syncs it like any other commit.

Dry-run computes the full plan (read-only API calls) and stops before the first write, so the
evidence shows exactly what live mode would have done.
"""

import asyncio
import logging
from typing import Any

import httpx
from pydantic import BaseModel

from .actions import ActionKind, ActionStatus, Decision, Tier

log = logging.getLogger("assistant.executor")

GITHUB_API = "https://api.github.com"


class RevertPlan(BaseModel):
    revision: str
    parent: str
    files: list[tuple[str, str]] = []  # (path, blob sha to restore)
    refused: str | None = None

    def describe(self) -> str:
        if self.refused:
            return f"revert of {self.revision[:7]} refused: {self.refused}"
        paths = ", ".join(p for p, _ in self.files)
        return f"revert {self.revision[:7]} → restore {paths} from {self.parent[:7]}"


def overlay_path(namespace: str) -> str:
    """sentinel-staging → apps/overlays/staging/ (the only path a revert may touch)."""
    return f"apps/overlays/{namespace.removeprefix('sentinel-')}/"


class GitOpsRepo:
    def __init__(
        self,
        http: httpx.AsyncClient,
        repo: str,
        token: str | None,
        branch: str = "main",
        api: str = GITHUB_API,
    ) -> None:
        self._http, self._repo, self._branch, self._api = http, repo, branch, api
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"  # a credential: never logged
        self.can_write = bool(token)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        r = await self._http.request(
            method, f"{self._api}/repos/{self._repo}{path}", headers=self._headers, **kwargs
        )
        r.raise_for_status()
        return r.json() if r.content else None

    async def plan_revert(self, revision: str, namespace: str) -> RevertPlan:
        commit = await self._call("GET", f"/commits/{revision}")
        parents = [p["sha"] for p in commit.get("parents", [])]
        if not parents:
            return RevertPlan(revision=revision, parent="", refused="the commit has no parent")
        plan = RevertPlan(revision=commit["sha"], parent=parents[0])
        diff = await self._call("GET", f"/compare/{plan.parent}...{plan.revision}")
        allowed = overlay_path(namespace)
        for f in diff.get("files", []):
            path = f["filename"]
            if not path.startswith(allowed):
                plan.refused = f"it touches {path}, outside {allowed}"
                return plan
            if f.get("status") != "modified":
                plan.refused = f"{path} was {f.get('status')}, not modified"
                return plan
            current = await self._call("GET", f"/contents/{path}", params={"ref": self._branch})
            if current["sha"] != f["sha"]:
                plan.refused = f"{path} has changed since {plan.revision[:7]} (already undone?)"
                return plan
            before = await self._call("GET", f"/contents/{path}", params={"ref": plan.parent})
            plan.files.append((path, before["sha"]))
        if not plan.files:
            plan.refused = "the commit changes no files"
        return plan

    async def open_revert_pr(self, plan: RevertPlan, incident_id: str, reasons: list[str]) -> dict:
        """Branch + commit + PR from the parent's blobs. Returns the PR (number, html_url)."""
        head = await self._call("GET", f"/git/ref/heads/{self._branch}")
        base_sha = head["object"]["sha"]
        base_commit = await self._call("GET", f"/git/commits/{base_sha}")
        tree = await self._call(
            "POST",
            "/git/trees",
            json={
                "base_tree": base_commit["tree"]["sha"],
                "tree": [
                    {"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in plan.files
                ],
            },
        )
        message = (
            f"revert: {plan.revision[:7]} (sentinel-assistant, incident {incident_id})\n\n"
            + "\n".join(f"- {r}" for r in reasons)
        )
        commit = await self._call(
            "POST",
            "/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
        )
        branch = f"sentinel/revert-{plan.revision[:7]}-{incident_id[:15]}"
        await self._call(
            "POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]}
        )
        body = (
            f"Proposed by sentinel-assistant for incident `{incident_id}`.\n\n"
            f"Restores {', '.join(p for p, _ in plan.files)} to {plan.parent[:7]}, undoing "
            f"{plan.revision[:7]}.\n\nWhy (policy reasons, not model text):\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\n\nMerging this PR is the approval. Closing it rejects the proposal."
        )
        return await self._call(
            "POST",
            "/pulls",
            json={
                "title": f"revert {plan.revision[:7]} (incident {incident_id})",
                "head": branch,
                "base": self._branch,
                "body": body,
            },
        )

    async def merge(self, number: int) -> None:
        await self._call("PUT", f"/pulls/{number}/merge", json={"merge_method": "merge"})

    async def pr_state(self, number: int) -> tuple[str, str | None]:
        """('open' | 'merged' | 'closed', who merged it)."""
        pr = await self._call("GET", f"/pulls/{number}")
        if pr.get("merged"):
            return "merged", (pr.get("merged_by") or {}).get("login")
        return pr["state"], None


class RolloutsApi:
    def __init__(self, http: httpx.AsyncClient, api_url: str) -> None:
        self._http, self._url = http, api_url.rstrip("/")

    async def abort(self, namespace: str, rollout: str) -> None:
        r = await self._http.patch(
            f"{self._url}/apis/argoproj.io/v1alpha1/namespaces/{namespace}/rollouts/"
            f"{rollout}/status",
            content=b'{"status":{"abort":true}}',
            headers={"Content-Type": "application/merge-patch+json"},
        )
        r.raise_for_status()


class Executor:
    def __init__(self, gitops: GitOpsRepo | None, rollouts: RolloutsApi | None) -> None:
        self._gitops, self._rollouts = gitops, rollouts

    async def run(self, decision: Decision, incident_id: str, dry_run: bool) -> None:
        """Fill decision.result (and status, when live). Never raises: a failed action is a
        FAILED decision with the reason, reported like everything else."""
        try:
            await self._run(decision, incident_id, dry_run)
        except Exception as exc:  # noqa: BLE001 - recorded on the decision and audited
            decision.result = f"{type(exc).__name__}: {str(exc)[:200]}"
            if not dry_run:
                decision.status = ActionStatus.FAILED
            log.warning("incident %s: %s failed: %s", incident_id, decision.action, decision.result)

    async def _run(self, decision: Decision, incident_id: str, dry_run: bool) -> None:
        t = decision.target
        live = not dry_run and decision.status in (
            ActionStatus.READY,
            ActionStatus.AWAITING_APPROVAL,
        )
        if decision.action is ActionKind.ABORT_CANARY:
            what = f"PATCH rollouts/{t['rollout']}/status abort=true in {t['namespace']}"
            if not live or decision.status is ActionStatus.AWAITING_APPROVAL:
                # Tier 2 abort (suspected injection): a human runs it; no Slack button yet.
                decision.result = f"would {what}" if dry_run else f"for a human: {what}"
                return
            if self._rollouts is None:
                raise RuntimeError("no Kubernetes API configured")
            await self._rollouts.abort(t["namespace"], t["rollout"])
            decision.status, decision.result = ActionStatus.EXECUTED, what
        elif decision.action is ActionKind.REVERT_COMMIT:
            if self._gitops is None:
                raise RuntimeError("no gitops repository configured")
            plan = await self._gitops.plan_revert(t["revision"], t["namespace"])
            decision.target = {**t, "plan": plan.model_dump()}
            if plan.refused:
                decision.result = plan.describe()
                if live:
                    decision.status, decision.blocked_by = ActionStatus.BLOCKED, plan.refused
                return
            if not live:
                decision.result = f"would open a PR: {plan.describe()}" + (
                    " and merge it" if decision.tier is Tier.ACT else ", for a human to merge"
                )
                return
            if not self._gitops.can_write:
                raise RuntimeError("no GitHub token: cannot open the revert PR")
            pr = await self._gitops.open_revert_pr(plan, incident_id, decision.reasons)
            decision.target = {**decision.target, "pr": pr["number"], "pr_url": pr["html_url"]}
            if decision.status is ActionStatus.READY:
                await self._gitops.merge(pr["number"])
                decision.status = ActionStatus.EXECUTED
                decision.result = f"merged PR #{pr['number']}: {plan.describe()}"
            else:
                decision.result = f"PR #{pr['number']} opened, waiting for a human to merge it"
        elif decision.action is ActionKind.SCALE_OUT:
            decision.result = "proposal only: scale-out is not automated; a human decides"

    async def await_approval(
        self, decision: Decision, poll_seconds: float, timeout_seconds: float
    ) -> bool:
        """For a tier 2 revert PR: wait until a human merges (approved) or closes it (rejected),
        or the time runs out (expired, the PR stays open). True if the state changed."""
        number = decision.target.get("pr")
        if self._gitops is None or decision.status is not ActionStatus.AWAITING_APPROVAL:
            return False
        if not number:
            return False
        waited = 0.0
        while waited < timeout_seconds:
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            try:
                state, who = await self._gitops.pr_state(number)
            except httpx.HTTPError as exc:
                log.warning("PR #%s: state check failed: %r", number, exc)
                continue
            if state == "merged":
                decision.status, decision.approver = ActionStatus.APPROVED, who
                decision.result = f"PR #{number} merged by {who}: approved; Argo CD syncs it"
                return True
            if state == "closed":
                decision.status = ActionStatus.REJECTED
                decision.result = f"PR #{number} closed without merging: rejected"
                return True
        decision.result = f"PR #{number}: no decision within {int(timeout_seconds // 60)} min"
        return True
