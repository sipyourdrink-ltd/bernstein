"""Issue -> plan-comment -> PR pipeline with diff-revision-from-comments.

A four-stage orchestration that converts a GitHub issue into a merged-ready
pull request without dragging the operator through copy-paste:

    1. ``plan``      - agent reads the issue, writes a markdown plan,
                       posts it as a sticky comment on the issue.
    2. ``approval``  - poll the issue for a thumbs-up reaction or an
                       ``[approved]`` keyword from a configured user.
    3. ``pr_open``   - open a draft pull request with the proposed diff
                       and link back to the plan comment.
    4. ``pr_revise`` - read inline review comments newer than the last
                       revision marker, dispatch an agent with the
                       comments as context, push an updated commit.

The pipeline is a thin orchestration layer over existing primitives:

* GitHub I/O is funneled through a ``gh`` CLI wrapper similar to the one
  used by :mod:`bernstein.core.review_responder`.
* Each stage writes a state marker so re-running the pipeline is safe.
  Markers are HTML comments embedded inside the sticky plan-comment body
  (read with regex) plus an issue label per stage.
* The actual diff generation is delegated to an injected callable so the
  pipeline can be wired to the autofix daemon, the handoff bus, or any
  other agent driver without this module depending on them at import
  time.

The pipeline never auto-merges the resulting PR; merge gating stays with
the operator.

Idempotency
-----------

Every stage performs a *read-modify-write* against the GitHub-side state
markers.  Re-running a stage that has already completed returns the
existing result rather than producing a duplicate comment, label, or
pull request.  This lets the orchestrator drive the pipeline from a
crontab or systemd timer without bookkeeping.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public sentinels and state markers
# ---------------------------------------------------------------------------

#: HTML-comment marker embedded inside the sticky plan comment.  The
#: pipeline locates its own comment by searching the issue thread for
#: this marker; do not change the value without a migration.
STICKY_MARKER: str = "<!-- bernstein:issue-to-pr:plan -->"

#: Per-stage marker tokens stored inside the sticky comment body.
STAGE_PLAN_DONE_MARKER: str = "<!-- stage:plan:done -->"
STAGE_APPROVED_MARKER: str = "<!-- stage:approval:granted -->"
STAGE_PR_OPENED_MARKER: str = "<!-- stage:pr-open:done pr={pr_number} -->"
STAGE_PR_OPENED_RE: re.Pattern[str] = re.compile(
    r"<!--\s*stage:pr-open:done\s+pr=(\d+)\s*-->",
)
STAGE_PR_REVISED_MARKER: str = "<!-- stage:pr-revise:last={iso} sha={sha} -->"
STAGE_PR_REVISED_RE: re.Pattern[str] = re.compile(
    r"<!--\s*stage:pr-revise:last=([^\s]+)\s+sha=([^\s]+)\s*-->",
)

#: Default keyword that, posted by an authorised user, grants approval.
APPROVAL_KEYWORD: str = "[approved]"


class Stage(StrEnum):
    """Pipeline stages, ordered."""

    PLAN = "plan"
    APPROVAL = "approval"
    PR_OPEN = "pr_open"
    PR_REVISE = "pr_revise"


class StageOutcome(StrEnum):
    """Outcome of running a single stage."""

    ADVANCED = "advanced"
    """The stage produced new side effects; the pipeline moves on."""

    ALREADY_DONE = "already_done"
    """A marker shows this stage was completed previously; nothing to do."""

    WAITING = "waiting"
    """The stage cannot progress yet (e.g. awaiting human approval)."""

    SKIPPED = "skipped"
    """A trigger gate (label, author allow-list) declined the issue."""

    ERROR = "error"
    """The stage hit an unrecoverable error; the operator must intervene."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Triggers:
    """Gating conditions that decide whether the pipeline acts on an issue.

    Attributes:
        label_required: Issue label that must be present for the pipeline
            to engage. ``None`` disables the gate (every issue qualifies).
        author_allow_list: GitHub logins whose issues the pipeline will
            pick up. Empty tuple disables the gate.
    """

    label_required: str | None = None
    author_allow_list: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stages:
    """Per-stage behavioural toggles.

    Attributes:
        plan_comment_required_approval: When True, the pipeline pauses
            after posting the plan comment and waits for a thumbs-up
            reaction or the approval keyword.  When False the pipeline
            advances straight to PR-open after plan-comment posting.
        draft_pr_default: When True (default) the PR is opened in draft
            state and is converted to ready-for-review only by an
            explicit operator action elsewhere.
        approval_keyword: Keyword that, when posted by a user on the
            allow-list, grants approval.  Defaults to ``[approved]``.
        revise_quiet_window_s: Minimum age (seconds) of an inline review
            comment before the revision stage picks it up.  Mirrors the
            review-responder quiet window so reviewers can amend their
            comment without race conditions.
    """

    plan_comment_required_approval: bool = True
    draft_pr_default: bool = True
    approval_keyword: str = APPROVAL_KEYWORD
    revise_quiet_window_s: float = 60.0


@dataclass(frozen=True)
class RepoRef:
    """Identifies a single ``owner/name`` repository the pipeline drives.

    Attributes:
        owner: GitHub user or organisation slug.
        name: Repository name (without owner prefix).
    """

    owner: str
    name: str

    @property
    def slug(self) -> str:
        """Return the ``owner/name`` string accepted by the ``gh`` CLI."""
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class IssueToPRConfig:
    """Top-level configuration for the pipeline.

    Attributes:
        repos: Repositories that participate.  Issues from other repos
            are ignored even if a webhook delivers them.
        triggers: Trigger gates that filter issues before the pipeline
            spends any agent budget.
        stages: Per-stage toggles.
    """

    repos: tuple[RepoRef, ...] = ()
    triggers: Triggers = field(default_factory=Triggers)
    stages: Stages = field(default_factory=Stages)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> IssueToPRConfig:
        """Build a config from the ``orchestration.issue_to_pr`` block.

        Accepts the shape documented in ``docs/orchestration/issue-to-pr.md``::

            orchestration:
              issue_to_pr:
                repos:
                  - {owner: acme, name: web}
                triggers:
                  label_required: ai-welcome
                  author_allow_list: [alice, bob]
                stages:
                  plan_comment_required_approval: true
                  draft_pr_default: true

        Unknown keys are ignored to keep forward compatibility cheap.
        """
        payload = payload or {}
        raw_repos: Iterable[dict[str, Any]] = payload.get("repos") or ()
        repos = tuple(
            RepoRef(owner=str(r["owner"]), name=str(r["name"]))
            for r in raw_repos
            if isinstance(r, dict) and "owner" in r and "name" in r
        )
        trig = payload.get("triggers") or {}
        triggers = Triggers(
            label_required=trig.get("label_required") or None,
            author_allow_list=tuple(trig.get("author_allow_list") or ()),
        )
        st = payload.get("stages") or {}
        stages = Stages(
            plan_comment_required_approval=bool(
                st.get("plan_comment_required_approval", True),
            ),
            draft_pr_default=bool(st.get("draft_pr_default", True)),
            approval_keyword=str(st.get("approval_keyword") or APPROVAL_KEYWORD),
            revise_quiet_window_s=float(st.get("revise_quiet_window_s", 60.0)),
        )
        return cls(repos=repos, triggers=triggers, stages=stages)


# ---------------------------------------------------------------------------
# Plan / diff payloads (injection points)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueContext:
    """Minimal issue payload handed to the plan generator.

    Attributes:
        repo: ``owner/repo`` slug.
        number: Issue number.
        title: Issue title.
        body: Issue body (markdown).
        author: GitHub login of the issue author.
        labels: Tuple of labels currently on the issue.
    """

    repo: str
    number: int
    title: str
    body: str
    author: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanProposal:
    """Plan-comment payload produced by an injected agent.

    Attributes:
        markdown: Markdown body to be posted as the sticky plan comment.
            The pipeline wraps this in the sticky marker; callers must
            NOT include the marker themselves.
        branch: Branch name that the diff stage will push to.
        commit_message: Single-line commit subject the diff stage uses.
    """

    markdown: str
    branch: str
    commit_message: str


@dataclass(frozen=True)
class DiffProposal:
    """Diff payload handed to the PR-open / PR-revise stages.

    Attributes:
        patch: Unified-diff text (``git diff`` output) for the changes.
            Empty string means "no changes"; callers should treat this
            as a no-op and not open a PR.
        branch: Branch the diff is targeted at.
        commit_message: Commit subject; the body is appended by the
            pipeline with a back-reference to the issue and plan comment.
        base: Base branch the PR is opened against. Defaults to ``main``.
    """

    patch: str
    branch: str
    commit_message: str
    base: str = "main"


#: Type aliases for the two injection points.
PlanGenerator = Callable[[IssueContext], PlanProposal]
DiffGenerator = Callable[[IssueContext, PlanProposal], DiffProposal]
ReviseGenerator = Callable[
    [IssueContext, int, list["InlineReviewComment"]],
    DiffProposal,
]


@dataclass(frozen=True)
class InlineReviewComment:
    """Inline review comment normalised for the revise stage.

    Attributes:
        comment_id: GitHub review-comment id.
        path: File path the comment is anchored to.
        line: 1-based line number (uses ``line`` or ``original_line``).
        body: Comment body markdown.
        reviewer: GitHub login of the reviewer.
        created_at: ISO 8601 timestamp.
    """

    comment_id: int
    path: str
    line: int
    body: str
    reviewer: str
    created_at: str


# ---------------------------------------------------------------------------
# Thin ``gh`` CLI wrapper
# ---------------------------------------------------------------------------


GhRunner = Callable[[list[str], "str | None"], subprocess.CompletedProcess[str]]


def _default_runner(
    args: list[str],
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` with optional stdin, capturing stdout/stderr."""
    return subprocess.run(  # nosec B603 - args constructed by caller
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


@dataclass
class IssuePRClient:
    """Tiny ``gh`` wrapper for the issue-to-PR pipeline.

    Centralised so tests can stub one runner instead of patching
    ``subprocess.run`` in every test.
    """

    runner: GhRunner = _default_runner

    # -- reads ----------------------------------------------------------

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        """Fetch issue payload via the REST API.

        Returns the parsed JSON dict on success, an empty dict on
        failure.  Callers MUST check for missing keys before use.
        """
        result = self.runner(
            ["api", f"repos/{repo}/issues/{number}"],
            None,
        )
        if result.returncode != 0:
            logger.warning(
                "gh issues get failed for %s#%d: %s",
                repo,
                number,
                result.stderr.strip(),
            )
            return {}
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def list_issue_comments(
        self,
        repo: str,
        number: int,
    ) -> list[dict[str, Any]]:
        """List the issue's comment thread.

        Returns the raw GitHub payload list; empty on failure.
        """
        result = self.runner(
            ["api", f"repos/{repo}/issues/{number}/comments?per_page=100"],
            None,
        )
        if result.returncode != 0:
            logger.warning(
                "gh comments list failed for %s#%d: %s",
                repo,
                number,
                result.stderr.strip(),
            )
            return []
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def list_issue_reactions(
        self,
        repo: str,
        number: int,
    ) -> list[dict[str, Any]]:
        """List reactions on the issue body."""
        result = self.runner(
            [
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{repo}/issues/{number}/reactions?per_page=100",
            ],
            None,
        )
        if result.returncode != 0:
            return []
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def list_pr_review_comments(
        self,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        """List inline review comments on a PR."""
        result = self.runner(
            ["api", f"repos/{repo}/pulls/{pr_number}/comments?per_page=100"],
            None,
        )
        if result.returncode != 0:
            return []
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    # -- writes ---------------------------------------------------------

    def post_issue_comment(
        self,
        *,
        repo: str,
        number: int,
        body: str,
    ) -> dict[str, Any] | None:
        """Post a new issue comment, returning the created payload."""
        payload = json.dumps({"body": body})
        result = self.runner(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues/{number}/comments",
                "--input",
                "-",
            ],
            payload,
        )
        if result.returncode != 0:
            logger.warning(
                "gh post comment failed: %s",
                result.stderr.strip(),
            )
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def patch_issue_comment(
        self,
        *,
        repo: str,
        comment_id: int,
        body: str,
    ) -> bool:
        """Edit an existing issue comment in place."""
        payload = json.dumps({"body": body})
        result = self.runner(
            [
                "api",
                "-X",
                "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--input",
                "-",
            ],
            payload,
        )
        return result.returncode == 0

    def open_pull_request(
        self,
        *,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> dict[str, Any] | None:
        """Open a pull request via the REST API.

        Returns the created PR payload on success; ``None`` on failure.
        """
        payload = json.dumps(
            {
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "draft": draft,
            }
        )
        result = self.runner(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repo}/pulls",
                "--input",
                "-",
            ],
            payload,
        )
        if result.returncode != 0:
            logger.warning(
                "gh open pr failed: %s",
                result.stderr.strip(),
            )
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def find_sticky_comment(
    comments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the sticky plan comment from a thread, or ``None``."""
    for c in comments:
        body = c.get("body") or ""
        if STICKY_MARKER in body:
            return c
    return None


def has_marker(body: str, marker: str) -> bool:
    """Return True iff ``marker`` appears inside ``body``."""
    return marker in body


def extract_pr_number(body: str) -> int | None:
    """Read the PR number from a ``pr-open`` marker, if present."""
    m = STAGE_PR_OPENED_RE.search(body)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def extract_last_revise(body: str) -> tuple[str, str] | None:
    """Return ``(iso_timestamp, sha)`` from a ``pr-revise`` marker."""
    m = STAGE_PR_REVISED_RE.search(body)
    if not m:
        return None
    return (m.group(1), m.group(2))


def build_plan_body(plan_markdown: str) -> str:
    """Wrap a raw plan markdown in the sticky marker and plan-done tag.

    Args:
        plan_markdown: Author-supplied markdown.

    Returns:
        Body string ready to be posted as an issue comment.
    """
    return f"{STICKY_MARKER}\n{STAGE_PLAN_DONE_MARKER}\n\n{plan_markdown.strip()}\n"


def append_marker(body: str, marker: str) -> str:
    """Append ``marker`` to the end of ``body`` if not already present."""
    if marker in body:
        return body
    suffix = "\n" if not body.endswith("\n") else ""
    return f"{body}{suffix}{marker}\n"


# ---------------------------------------------------------------------------
# Pipeline trace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageReport:
    """Outcome of advancing one stage during a tick."""

    stage: Stage
    outcome: StageOutcome
    detail: str = ""
    pr_number: int | None = None


@dataclass(frozen=True)
class PipelineTrace:
    """Pipeline state snapshot, returned by :meth:`IssueToPRPipeline.trace`."""

    repo: str
    issue_number: int
    plan_posted: bool
    approved: bool
    pr_number: int | None
    last_revise_at: str | None

    def render(self) -> str:
        """Format a human-readable summary for ``bernstein issue-to-pr trace``."""
        lines = [
            f"repo:           {self.repo}",
            f"issue:          #{self.issue_number}",
            f"plan_posted:    {self.plan_posted}",
            f"approved:       {self.approved}",
            f"pr_number:      {self.pr_number if self.pr_number is not None else '-'}",
            f"last_revise_at: {self.last_revise_at or '-'}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The pipeline itself
# ---------------------------------------------------------------------------


@dataclass
class IssueToPRPipeline:
    """Orchestrate an issue through the four-stage pipeline.

    The pipeline is intentionally side-effect-light at construction time:
    no network is touched until a ``tick_*`` method runs.  Callers wire
    the agent-side plan and diff generation in via the three callables.

    Attributes:
        config: Behavioural configuration.
        client: ``gh`` CLI wrapper.
        plan_generator: Callable that turns an :class:`IssueContext` into
            a :class:`PlanProposal`.
        diff_generator: Callable that produces the initial diff for a
            previously-approved plan.
        revise_generator: Callable that consumes new inline review
            comments and produces a follow-up diff.
        apply_diff: Callable that, given a :class:`DiffProposal`, applies
            the patch to a working tree and pushes the branch to the
            remote, returning the head commit SHA.  Defaults to a stub
            that raises; production wiring injects the autofix-daemon
            applicator.
    """

    config: IssueToPRConfig
    client: IssuePRClient = field(default_factory=IssuePRClient)
    plan_generator: PlanGenerator | None = None
    diff_generator: DiffGenerator | None = None
    revise_generator: ReviseGenerator | None = None
    apply_diff: Callable[[DiffProposal], str] | None = None

    # -- gating ---------------------------------------------------------

    def _is_repo_allowed(self, repo: str) -> bool:
        if not self.config.repos:
            # No allow-list configured: accept any repo.  This keeps
            # tests cheap while still letting operators lock the
            # pipeline down by setting ``repos:`` explicitly.
            return True
        return any(r.slug == repo for r in self.config.repos)

    def _is_issue_eligible(self, issue: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(ok, reason)`` describing whether the issue qualifies."""
        labels = tuple(lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict))
        required = self.config.triggers.label_required
        if required and required not in labels:
            return False, f"missing required label {required!r}"
        allow = self.config.triggers.author_allow_list
        if allow:
            user = (issue.get("user") or {}).get("login")
            if user not in allow:
                return False, f"author {user!r} not in allow_list"
        return True, ""

    # -- stage 1: plan --------------------------------------------------

    def tick_plan(self, repo: str, issue_number: int) -> StageReport:
        """Run the plan-comment stage exactly once."""
        if not self._is_repo_allowed(repo):
            return StageReport(
                Stage.PLAN,
                StageOutcome.SKIPPED,
                detail=f"repo {repo!r} not in allow list",
            )
        issue = self.client.get_issue(repo, issue_number)
        if not issue:
            return StageReport(
                Stage.PLAN,
                StageOutcome.ERROR,
                detail="issue payload empty (gh failure?)",
            )
        ok, reason = self._is_issue_eligible(issue)
        if not ok:
            return StageReport(Stage.PLAN, StageOutcome.SKIPPED, detail=reason)

        comments = self.client.list_issue_comments(repo, issue_number)
        sticky = find_sticky_comment(comments)
        if sticky and has_marker(sticky.get("body") or "", STAGE_PLAN_DONE_MARKER):
            return StageReport(
                Stage.PLAN,
                StageOutcome.ALREADY_DONE,
                detail=f"sticky comment id={sticky.get('id')}",
            )
        if self.plan_generator is None:
            return StageReport(
                Stage.PLAN,
                StageOutcome.ERROR,
                detail="plan_generator not configured",
            )
        ctx = IssueContext(
            repo=repo,
            number=issue_number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            author=str((issue.get("user") or {}).get("login") or ""),
            labels=tuple(lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict)),
        )
        proposal = self.plan_generator(ctx)
        body = build_plan_body(proposal.markdown)
        posted = self.client.post_issue_comment(
            repo=repo,
            number=issue_number,
            body=body,
        )
        if posted is None:
            return StageReport(
                Stage.PLAN,
                StageOutcome.ERROR,
                detail="failed to post sticky comment",
            )
        return StageReport(
            Stage.PLAN,
            StageOutcome.ADVANCED,
            detail=f"sticky comment id={posted.get('id')}",
        )

    # -- stage 2: approval ---------------------------------------------

    def tick_approval(self, repo: str, issue_number: int) -> StageReport:
        """Check whether approval has been granted for the plan."""
        if not self.config.stages.plan_comment_required_approval:
            return StageReport(
                Stage.APPROVAL,
                StageOutcome.ADVANCED,
                detail="approval not required",
            )
        comments = self.client.list_issue_comments(repo, issue_number)
        sticky = find_sticky_comment(comments)
        if sticky is None:
            return StageReport(
                Stage.APPROVAL,
                StageOutcome.WAITING,
                detail="plan comment not posted yet",
            )
        body = sticky.get("body") or ""
        if has_marker(body, STAGE_APPROVED_MARKER):
            return StageReport(
                Stage.APPROVAL,
                StageOutcome.ALREADY_DONE,
                detail="approval marker present",
            )

        if self._approval_keyword_seen(comments):
            return self._mark_approved(repo, sticky)

        if self._thumbs_up_seen(repo, sticky.get("id")):
            return self._mark_approved(repo, sticky)

        return StageReport(
            Stage.APPROVAL,
            StageOutcome.WAITING,
            detail="no approval signal yet",
        )

    def _approval_keyword_seen(self, comments: list[dict[str, Any]]) -> bool:
        keyword = self.config.stages.approval_keyword
        allow = set(self.config.triggers.author_allow_list)
        for c in comments:
            body = (c.get("body") or "").strip()
            if keyword in body:
                user = (c.get("user") or {}).get("login")
                if not allow or user in allow:
                    return True
        return False

    def _thumbs_up_seen(self, repo: str, comment_id: Any) -> bool:
        if comment_id is None:
            return False
        result = self.client.runner(
            [
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{repo}/issues/comments/{comment_id}/reactions?per_page=100",
            ],
            None,
        )
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return False
        if not isinstance(data, list):
            return False
        allow = set(self.config.triggers.author_allow_list)
        for r in data:
            if not isinstance(r, dict):
                continue
            if r.get("content") != "+1":
                continue
            user = (r.get("user") or {}).get("login")
            if not allow or user in allow:
                return True
        return False

    def _mark_approved(
        self,
        repo: str,
        sticky: dict[str, Any],
    ) -> StageReport:
        body = sticky.get("body") or ""
        new_body = append_marker(body, STAGE_APPROVED_MARKER)
        ok = self.client.patch_issue_comment(
            repo=repo,
            comment_id=int(sticky.get("id") or 0),
            body=new_body,
        )
        if not ok:
            return StageReport(
                Stage.APPROVAL,
                StageOutcome.ERROR,
                detail="failed to write approval marker",
            )
        return StageReport(
            Stage.APPROVAL,
            StageOutcome.ADVANCED,
            detail="approval recorded",
        )

    # -- stage 3: pr_open ----------------------------------------------

    def tick_pr_open(self, repo: str, issue_number: int) -> StageReport:
        """Open the draft pull request once approval is in place."""
        comments = self.client.list_issue_comments(repo, issue_number)
        sticky = find_sticky_comment(comments)
        if sticky is None:
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.WAITING,
                detail="plan comment not posted yet",
            )
        body = sticky.get("body") or ""
        existing_pr = extract_pr_number(body)
        if existing_pr is not None:
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.ALREADY_DONE,
                detail=f"PR #{existing_pr} already linked",
                pr_number=existing_pr,
            )
        if self.config.stages.plan_comment_required_approval and not has_marker(body, STAGE_APPROVED_MARKER):
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.WAITING,
                detail="awaiting approval marker",
            )
        if self.diff_generator is None or self.apply_diff is None:
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.ERROR,
                detail="diff_generator or apply_diff not configured",
            )

        issue = self.client.get_issue(repo, issue_number)
        if not issue:
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.ERROR,
                detail="issue payload empty",
            )
        ctx = IssueContext(
            repo=repo,
            number=issue_number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            author=str((issue.get("user") or {}).get("login") or ""),
            labels=tuple(lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict)),
        )
        plan_proposal = PlanProposal(
            markdown="",
            branch=f"bernstein/issue-{issue_number}",
            commit_message=f"feat: resolve #{issue_number}",
        )
        diff = self.diff_generator(ctx, plan_proposal)
        if not diff.patch.strip():
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.WAITING,
                detail="diff is empty; no PR opened",
            )
        try:
            self.apply_diff(diff)
        except Exception as exc:
            logger.exception("apply_diff failed for %s#%d", repo, issue_number)
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.ERROR,
                detail=f"apply_diff raised: {exc!r}",
            )
        pr_body = f"Resolves #{issue_number}.\n\nPlan: see comment #{sticky.get('id')} on the issue.\n"
        title = diff.commit_message or f"feat: resolve #{issue_number}"
        pr = self.client.open_pull_request(
            repo=repo,
            head=diff.branch,
            base=diff.base,
            title=title,
            body=pr_body,
            draft=self.config.stages.draft_pr_default,
        )
        if pr is None or "number" not in pr:
            return StageReport(
                Stage.PR_OPEN,
                StageOutcome.ERROR,
                detail="open_pull_request returned no payload",
            )
        pr_number = int(pr["number"])
        marker = STAGE_PR_OPENED_MARKER.format(pr_number=pr_number)
        new_body = append_marker(body, marker)
        self.client.patch_issue_comment(
            repo=repo,
            comment_id=int(sticky.get("id") or 0),
            body=new_body,
        )
        return StageReport(
            Stage.PR_OPEN,
            StageOutcome.ADVANCED,
            detail=f"opened PR #{pr_number}",
            pr_number=pr_number,
        )

    # -- stage 4: pr_revise --------------------------------------------

    def tick_pr_revise(self, repo: str, issue_number: int) -> StageReport:
        """Apply a follow-up revision driven by inline review comments."""
        comments = self.client.list_issue_comments(repo, issue_number)
        sticky = find_sticky_comment(comments)
        if sticky is None:
            return StageReport(
                Stage.PR_REVISE,
                StageOutcome.WAITING,
                detail="no sticky comment yet",
            )
        body = sticky.get("body") or ""
        pr_number = extract_pr_number(body)
        if pr_number is None:
            return StageReport(
                Stage.PR_REVISE,
                StageOutcome.WAITING,
                detail="PR not opened yet",
            )

        last = extract_last_revise(body)
        last_iso = last[0] if last else ""

        raw = self.client.list_pr_review_comments(repo, pr_number)
        new_comments = self._select_new_comments(raw, last_iso)
        if not new_comments:
            return StageReport(
                Stage.PR_REVISE,
                StageOutcome.ALREADY_DONE,
                detail="no new review comments",
                pr_number=pr_number,
            )
        if self.revise_generator is None or self.apply_diff is None:
            return StageReport(
                Stage.PR_REVISE,
                StageOutcome.ERROR,
                detail="revise_generator or apply_diff not configured",
                pr_number=pr_number,
            )
        issue = self.client.get_issue(repo, issue_number)
        ctx = IssueContext(
            repo=repo,
            number=issue_number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            author=str((issue.get("user") or {}).get("login") or ""),
            labels=tuple(lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict)),
        )
        diff = self.revise_generator(ctx, pr_number, new_comments)
        if not diff.patch.strip():
            return StageReport(
                Stage.PR_REVISE,
                StageOutcome.WAITING,
                detail="revise produced empty diff",
                pr_number=pr_number,
            )
        try:
            sha = self.apply_diff(diff)
        except Exception as exc:
            logger.exception(
                "apply_diff (revise) failed for %s#%d",
                repo,
                issue_number,
            )
            return StageReport(
                Stage.PR_REVISE,
                StageOutcome.ERROR,
                detail=f"apply_diff raised: {exc!r}",
                pr_number=pr_number,
            )
        newest_iso = max(c.created_at for c in new_comments)
        marker = STAGE_PR_REVISED_MARKER.format(iso=newest_iso, sha=sha)
        # Replace any prior revise marker rather than stacking them.
        cleaned_body = STAGE_PR_REVISED_RE.sub("", body).rstrip() + "\n"
        new_body = append_marker(cleaned_body, marker)
        self.client.patch_issue_comment(
            repo=repo,
            comment_id=int(sticky.get("id") or 0),
            body=new_body,
        )
        return StageReport(
            Stage.PR_REVISE,
            StageOutcome.ADVANCED,
            detail=f"revised PR #{pr_number} with {len(new_comments)} comment(s)",
            pr_number=pr_number,
        )

    def _select_new_comments(
        self,
        raw: list[dict[str, Any]],
        last_iso: str,
    ) -> list[InlineReviewComment]:
        out: list[InlineReviewComment] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            created = str(r.get("created_at") or "")
            if last_iso and created <= last_iso:
                continue
            line = r.get("line") or r.get("original_line") or 0
            try:
                line_int = int(line)
            except (TypeError, ValueError):
                continue
            out.append(
                InlineReviewComment(
                    comment_id=int(r.get("id") or 0),
                    path=str(r.get("path") or ""),
                    line=line_int,
                    body=str(r.get("body") or ""),
                    reviewer=str((r.get("user") or {}).get("login") or ""),
                    created_at=created,
                )
            )
        # Stable order: oldest first so the agent sees them in posting order.
        out.sort(key=lambda c: c.created_at)
        return out

    # -- driver --------------------------------------------------------

    def tick(self, repo: str, issue_number: int) -> list[StageReport]:
        """Advance every stage that can advance, in order.

        Returns the per-stage reports collected during the tick.  A stage
        that returns ``WAITING`` short-circuits the remaining stages so
        the operator can act before the pipeline burns budget on a stage
        that cannot succeed yet.
        """
        reports: list[StageReport] = []
        for stage_fn in (
            self.tick_plan,
            self.tick_approval,
            self.tick_pr_open,
            self.tick_pr_revise,
        ):
            report = stage_fn(repo, issue_number)
            reports.append(report)
            if report.outcome in (
                StageOutcome.WAITING,
                StageOutcome.SKIPPED,
                StageOutcome.ERROR,
            ):
                break
        return reports

    # -- trace ---------------------------------------------------------

    def trace(self, repo: str, issue_number: int) -> PipelineTrace:
        """Return the current pipeline state for ``bernstein issue-to-pr trace``."""
        comments = self.client.list_issue_comments(repo, issue_number)
        sticky = find_sticky_comment(comments)
        body = (sticky or {}).get("body") or ""
        plan_posted = sticky is not None and has_marker(body, STAGE_PLAN_DONE_MARKER)
        approved = has_marker(body, STAGE_APPROVED_MARKER)
        pr_number = extract_pr_number(body)
        last = extract_last_revise(body)
        return PipelineTrace(
            repo=repo,
            issue_number=issue_number,
            plan_posted=plan_posted,
            approved=approved,
            pr_number=pr_number,
            last_revise_at=last[0] if last else None,
        )


__all__ = [
    "APPROVAL_KEYWORD",
    "STAGE_APPROVED_MARKER",
    "STAGE_PLAN_DONE_MARKER",
    "STAGE_PR_OPENED_MARKER",
    "STAGE_PR_REVISED_MARKER",
    "STICKY_MARKER",
    "DiffGenerator",
    "DiffProposal",
    "InlineReviewComment",
    "IssueContext",
    "IssuePRClient",
    "IssueToPRConfig",
    "IssueToPRPipeline",
    "PipelineTrace",
    "PlanGenerator",
    "PlanProposal",
    "RepoRef",
    "ReviseGenerator",
    "Stage",
    "StageOutcome",
    "StageReport",
    "Stages",
    "Triggers",
    "append_marker",
    "build_plan_body",
    "extract_last_revise",
    "extract_pr_number",
    "find_sticky_comment",
    "has_marker",
]
