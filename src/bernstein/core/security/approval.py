"""Approval gates: configurable review step between janitor verification and merge.

Three modes:
  auto    - merge immediately after janitor passes (default, headless-friendly).
  review  - write a pending approval file, block until the user writes a decision
             file via ``bernstein approve <task_id>`` or ``bernstein reject <task_id>``.
  pr      - push the agent branch and create a GitHub PR; skip local merge.

Because the orchestrator runs as a background subprocess (stdout redirected to
log file), interactive terminal prompts are not viable. Instead, ``review`` mode
uses a file-based handshake:

  .sdd/runtime/pending_approvals/<task_id>.json   ← written by orchestrator
  .sdd/runtime/approvals/<task_id>.approved       ← written by ``bernstein approve``
  .sdd/runtime/approvals/<task_id>.rejected       ← written by ``bernstein reject``
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import APPROVAL

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = APPROVAL.poll_interval_s
_DEFAULT_MAX_WAIT_S = APPROVAL.max_wait_s


class ApprovalMode(StrEnum):
    """How the orchestrator handles work after janitor verification."""

    AUTO = "auto"
    REVIEW = "review"
    PR = "pr"


@dataclass
class ApprovalResult:
    """Decision returned by :class:`ApprovalGate.evaluate`.

    Attributes:
        approved: True if the work should be merged directly.
        rejected: True if the work was rejected (no merge, no PR).
        pr_url: Non-empty when a PR was created; implies approved=False, rejected=False.
    """

    approved: bool
    rejected: bool = False
    pr_url: str = ""


# ---------------------------------------------------------------------------
# File-based polling helper (production default; injectable for testing)
# ---------------------------------------------------------------------------


def _default_poll_decision(
    task_id: str,
    approvals_dir: Path,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    max_wait_s: float = _DEFAULT_MAX_WAIT_S,
    reject_on_timeout: bool = False,
) -> str:
    """Poll for a decision file and return ``"approved"`` or ``"rejected"``.

    Reads ``<approvals_dir>/<task_id>.approved`` or
    ``<approvals_dir>/<task_id>.rejected`` until one appears or the timeout
    expires.  On timeout, defaults to ``"approved"`` (or "rejected" if configured)
    so a missed review does not permanently stall the orchestrator.

    Args:
        task_id: Task ID to poll for.
        approvals_dir: Directory where decision files are written.
        poll_interval_s: Seconds between file-existence checks.
        max_wait_s: Maximum seconds to wait before defaulting to approved.
        reject_on_timeout: If True, returns "rejected" on timeout instead of "approved".

    Returns:
        ``"approved"`` or ``"rejected"``.
    """
    from bernstein.core.orchestration.approval_gate import approval_path_in

    # The same single implementation every other approvals sink resolves to,
    # in the variant that takes the directory rather than a project root.
    deadline = time.monotonic() + max_wait_s
    approved_path = approval_path_in(approvals_dir, task_id, ".approved")
    rejected_path = approval_path_in(approvals_dir, task_id, ".rejected")

    while time.monotonic() < deadline:
        if approved_path.exists():
            logger.info("Approval gate: task %s approved via file", task_id)
            return "approved"
        if rejected_path.exists():
            logger.info("Approval gate: task %s rejected via file", task_id)
            return "rejected"
        time.sleep(poll_interval_s)

    logger.warning(
        "Approval gate: task %s timed out after %.0fs - defaulting to %s",
        task_id,
        max_wait_s,
        "rejected" if reject_on_timeout else "approved",
    )
    return "rejected" if reject_on_timeout else "approved"


# ---------------------------------------------------------------------------
# ApprovalGate
# ---------------------------------------------------------------------------

_PollDecisionFn = Callable[..., str]
_PushBranchFn = Callable[..., Any]
_CreatePrFn = Callable[..., Any]


def _has_no_diff(worktree_path: Path, base_branch: str) -> bool:
    """Return True if the worktree has no diff vs the base branch."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", f"{base_branch}...HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _push_with_retry(push_fn: _PushBranchFn, worktree_path: Path, pr_branch: str, task_id: str) -> bool:
    """Push to remote with one retry on failure. Returns True on success."""
    push_result = push_fn(worktree_path, pr_branch)
    if getattr(push_result, "ok", True):
        return True

    stderr = getattr(push_result, "stderr", "")
    logger.warning("Approval gate: push failed for task %s, retrying: %s", task_id, stderr)
    import time as _time

    _time.sleep(2)
    push_result = push_fn(worktree_path, pr_branch)
    if not getattr(push_result, "ok", True):
        logger.error("Approval gate: push failed on retry for task %s", task_id)
        return False
    return True


def _try_enable_auto_merge(workdir: Path, pr_url: str) -> None:
    """Attempt to enable auto-merge on a PR, logging the outcome."""
    from bernstein.core.git_ops import enable_pr_auto_merge

    auto_result = enable_pr_auto_merge(workdir, pr_url)
    if auto_result.ok:
        logger.info("Approval gate: auto-merge enabled for PR %s", pr_url)
    else:
        logger.warning(
            "Approval gate: failed to enable auto-merge for PR %s: %s",
            pr_url,
            auto_result.stderr,
        )


class ApprovalGate:
    """Gate that decides whether a verified task's work should be merged.

    Args:
        mode: Approval mode (auto / review / pr).
        workdir: Repository root (used to locate .sdd/ state dirs).
        auto_merge: When True and a PR is created, enable auto-merge via ``gh pr merge --auto``.
        pr_labels: GitHub labels to apply to created PRs.
        _poll_decision: Injectable polling function for testing.  Signature:
            ``(task_id: str, approvals_dir: Path) -> str``.
        _push_branch_fn: Injectable push function for testing.
        _create_pr_fn: Injectable PR-creation function for testing.
    """

    def __init__(
        self,
        mode: ApprovalMode | str,
        workdir: Path,
        auto_merge: bool = True,
        pr_labels: list[str] | None = None,
        _poll_decision: _PollDecisionFn | None = None,
        _push_branch_fn: _PushBranchFn | None = None,
        _create_pr_fn: _CreatePrFn | None = None,
    ) -> None:
        self._mode = mode if isinstance(mode, ApprovalMode) else ApprovalMode(mode)
        self._workdir = workdir
        self._auto_merge = auto_merge
        self._pr_labels: list[str] = pr_labels if pr_labels is not None else ["bernstein", "auto-generated"]

        def _default_poll(
            task_id: str,
            approvals_dir: Path,
            max_wait_s: float = _DEFAULT_MAX_WAIT_S,
            reject_on_timeout: bool = False,
        ) -> str:
            return _default_poll_decision(
                task_id, approvals_dir, max_wait_s=max_wait_s, reject_on_timeout=reject_on_timeout
            )

        self._poll_decision: _PollDecisionFn = _poll_decision or _default_poll
        self._push_branch_fn = _push_branch_fn
        self._create_pr_fn = _create_pr_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        task: Task,
        *,
        session_id: str,
        diff: str = "",
        test_summary: str = "",
        override_mode: ApprovalMode | None = None,
        timeout_s: float | None = None,
        bypass_enabled: bool = False,
    ) -> ApprovalResult:
        """Evaluate the approval gate for a verified task.

        For ``auto``: immediately returns an approved result.
        For ``review``: writes a pending-approval file and blocks until the user
            signals a decision via a file.
        For ``pr``: immediately returns a non-approved, non-rejected result
            (caller should call :meth:`create_pr` separately).

        FAIL-CLOSED: any unexpected exception raised while resolving the
        decision (including inside ``_review``) is caught here, logged at
        ERROR with the full traceback and the inputs under evaluation, and
        turned into a REJECTED result. Callers must never see an exception
        escape this method and must never treat "gate raised" as "gate
        approved" -- see house lesson on ApprovalGate fail-open drift.

        Args:
            task: The completed task to review.
            session_id: Agent session ID (used for logging).
            diff: Optional unified diff string to include in the pending file.
            test_summary: Optional one-line test-results summary.
            override_mode: Optional mode to override the global configuration.
            timeout_s: Optional overriding timeout for review mode.
            bypass_enabled: When True, bypass approval and return approved=True.

        Returns:
            :class:`ApprovalResult` describing the decision.
        """
        try:
            if bypass_enabled:
                logger.info(
                    "Approval gate decision: task=%s session=%s decision=approved reason=bypass_enabled",
                    task.id,
                    session_id,
                )
                return ApprovalResult(approved=True)

            mode = override_mode if override_mode is not None else self._mode
            if mode == ApprovalMode.AUTO:
                logger.info(
                    "Approval gate decision: task=%s session=%s decision=approved reason=mode_auto",
                    task.id,
                    session_id,
                )
                return ApprovalResult(approved=True)

            if mode == ApprovalMode.PR:
                logger.info(
                    "Approval gate decision: task=%s session=%s decision=pending_pr reason=mode_pr",
                    task.id,
                    session_id,
                )
                return ApprovalResult(approved=False, rejected=False)

            # REVIEW mode
            result = self._review(
                task, session_id=session_id, diff=diff, test_summary=test_summary, timeout_s=timeout_s
            )
            logger.info(
                "Approval gate decision: task=%s session=%s decision=%s reason=review_mode_poll",
                task.id,
                session_id,
                "rejected" if result.rejected else "approved",
            )
            return result
        except Exception:
            logger.error(
                "Approval gate evaluate() raised -- FAIL-CLOSED to rejected (not auto-merge). "
                "task=%s session=%s override_mode=%s timeout_s=%s bypass_enabled=%s diff_len=%d "
                "test_summary=%r\n%s",
                task.id,
                session_id,
                override_mode,
                timeout_s,
                bypass_enabled,
                len(diff),
                test_summary,
                traceback.format_exc(),
            )
            return ApprovalResult(approved=False, rejected=True)

    def create_pr(
        self,
        task: Task,
        *,
        worktree_path: Path,
        session_id: str = "",
        base_branch: str = "main",
        labels: list[str] | None = None,
        _role: str = "",
        model: str = "",
        cost_usd: float = 0.0,
        test_summary: str = "",
    ) -> str:
        """Push the agent branch and open a GitHub PR.

        Pushes the current HEAD of the worktree to ``bernstein/task-{task.id}``
        on the remote (using a refspec so the local branch name is irrelevant),
        then creates a PR with a structured body including task metadata, cost,
        test results, and the agent role/model.

        NOTE ON PARAMETER NAMES: ``session_id``/``model``/``cost_usd`` are the
        public keyword names accepted for logging/metadata parity with other
        gate call sites; ``_role`` is underscore-prefixed by convention because
        it is "part of the interface" but not required for PR construction
        itself. Do NOT rename the public names without updating every caller --
        a prior signature drift where this method briefly accepted
        ``_session_id``/``_model``/``_cost_usd`` broke pre-existing callers and
        tests that pass ``session_id``/``model``/``cost_usd`` with
        ``TypeError: create_pr() got an unexpected keyword argument
        'session_id'``. This method fail-closes internally (below) so that even
        a *future* drift cannot escape as a bypass.

        Args:
            task: The task whose work should become a PR.
            worktree_path: Path to the agent's git worktree.
            session_id: Agent session ID (part of interface).
            base_branch: Target branch for the PR.
            labels: GitHub labels to attach (defaults to ["bernstein", "auto-generated"]).
            _role: Agent role (part of interface).
            model: Model name (part of interface).
            cost_usd: Cost in USD (part of interface).
            test_summary: One-line test result summary (e.g. ``"12 passed, 0 failed"``).

        Returns:
            PR URL on success, empty string on failure (failure -- including
            an internal exception -- must NEVER be treated by the caller as
            "approved"; it means no PR exists and merge must stay skipped).
        """
        try:
            return self._create_pr_inner(
                task,
                worktree_path=worktree_path,
                session_id=session_id,
                base_branch=base_branch,
                labels=labels,
                role=_role,
                model=model,
                cost_usd=cost_usd,
                test_summary=test_summary,
            )
        except Exception:
            logger.error(
                "Approval gate create_pr() raised -- FAIL-CLOSED, returning no PR (caller must NOT "
                "treat this as approved/auto-merge). task=%s session=%s role=%s model=%s cost_usd=%s "
                "base_branch=%s worktree_path=%s\n%s",
                task.id,
                session_id,
                _role,
                model,
                cost_usd,
                base_branch,
                worktree_path,
                traceback.format_exc(),
            )
            return ""

    def _create_pr_inner(
        self,
        task: Task,
        *,
        worktree_path: Path,
        session_id: str,
        base_branch: str,
        labels: list[str] | None,
        role: str,
        model: str,
        cost_usd: float,
        test_summary: str,
    ) -> str:
        """Do the actual push+PR-creation work. Exceptions propagate to create_pr()."""
        _ = session_id  # Part of interface (logging/metadata parity)
        _ = role  # Part of interface
        _ = model  # Part of interface
        _ = cost_usd  # Part of interface
        from bernstein.core.git_ops import PullRequestResult, create_github_pr, push_head_as

        effective_labels = labels if labels is not None else self._pr_labels
        pr_branch = f"bernstein/task-{task.id}"

        # Use push_head_as so the local branch name (agent/{session_id}) does
        # not matter - we publish the worktree HEAD as bernstein/task-{id}.
        push_fn: _PushBranchFn = self._push_branch_fn or push_head_as
        create_fn = self._create_pr_fn or create_github_pr

        if _has_no_diff(worktree_path, base_branch):
            logger.info(
                "Approval gate decision: task=%s decision=no_pr reason=no_diff_vs_%s",
                task.id,
                base_branch,
            )
            return ""

        if not _push_with_retry(push_fn, worktree_path, pr_branch, task.id):
            logger.info(
                "Approval gate decision: task=%s decision=no_pr reason=push_failed",
                task.id,
            )
            return ""

        diff_stats = self._get_diff_stats(worktree_path, base_branch)

        pr_result: PullRequestResult = create_fn(
            cwd=self._workdir,
            title=task.title,
            body=self._pr_body(task, test_summary=test_summary, diff_stats=diff_stats),
            head=pr_branch,
            base=base_branch,
            labels=effective_labels,
        )
        if not pr_result.success:
            logger.warning(
                "Approval gate decision: task=%s decision=no_pr reason=pr_create_failed error=%s",
                task.id,
                pr_result.error,
            )
            return ""

        logger.info(
            "Approval gate decision: task=%s decision=pr_created pr_url=%s",
            task.id,
            pr_result.pr_url,
        )
        if self._auto_merge and pr_result.pr_url:
            _try_enable_auto_merge(self._workdir, pr_result.pr_url)
        return pr_result.pr_url

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _review(
        self,
        task: Task,
        *,
        session_id: str,
        diff: str,
        test_summary: str,
        timeout_s: float | None = None,
    ) -> ApprovalResult:
        """Write pending file, block on poll, return decision."""
        pending_dir = self._workdir / ".sdd" / "runtime" / "pending_approvals"
        approvals_dir = self._workdir / ".sdd" / "runtime" / "approvals"
        pending_dir.mkdir(parents=True, exist_ok=True)
        approvals_dir.mkdir(parents=True, exist_ok=True)

        pending_file = pending_dir / f"{task.id}.json"
        payload: dict[str, str] = {
            "task_id": task.id,
            "task_title": task.title,
            "session_id": session_id,
            "diff": diff,
            "test_summary": test_summary,
        }
        pending_file.write_text(json.dumps(payload, indent=2))
        logger.info(
            "Approval gate: task %s pending review - run `bernstein approve %s` or `bernstein reject %s`",
            task.id,
            task.id,
            task.id,
        )

        kwargs: dict[str, Any] = {}
        if timeout_s is not None:
            kwargs["max_wait_s"] = timeout_s
            kwargs["reject_on_timeout"] = True

        decision = self._poll_decision(task.id, approvals_dir, **kwargs)

        if decision == "rejected":
            return ApprovalResult(approved=False, rejected=True)
        return ApprovalResult(approved=True)

    def _get_diff_stats(self, worktree_path: Path, base_branch: str) -> dict[str, Any]:
        """Get diff statistics for the PR body.

        Returns:
            Dict with 'files', 'insertions', 'deletions', 'file_list' keys.
        """
        stats: dict[str, Any] = {"files": 0, "insertions": 0, "deletions": 0, "file_list": []}

        try:
            stats["file_list"] = self._get_diff_file_list(worktree_path, base_branch)
            self._fill_shortstat(stats, worktree_path, base_branch)
        except Exception as exc:
            logger.debug("Failed to get diff stats: %s", exc)

        return stats

    @staticmethod
    def _get_diff_file_list(worktree_path: Path, base_branch: str) -> list[str]:
        """Extract the list of changed filenames from git diff --stat."""
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--stat", f"{base_branch}...HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        files: list[str] = []
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            for line in lines[:-1]:
                if "|" in line:
                    filename = line.split("|")[0].strip()
                    if filename:
                        files.append(filename)
        return files

    @staticmethod
    def _fill_shortstat(stats: dict[str, Any], worktree_path: Path, base_branch: str) -> None:
        """Parse git diff --shortstat and fill numeric stats."""
        import re
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--shortstat", f"{base_branch}...HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        text = result.stdout.strip()
        if m := re.search(r"(\d+) files? changed", text):
            stats["files"] = int(m.group(1))
        if m := re.search(r"(\d+) insertions?", text):
            stats["insertions"] = int(m.group(1))
        if m := re.search(r"(\d+) deletions?", text):
            stats["deletions"] = int(m.group(1))

    def _pr_body(
        self,
        task: Task,
        *,
        test_summary: str = "",
        diff_stats: dict[str, Any] | None = None,
    ) -> str:
        """Build a clean PR body with Summary and Changes sections."""
        lines = ["## Summary", ""]

        # Add task description as the summary
        if task.description:
            lines.append(task.description)
        else:
            lines.append(task.title)
        lines.append("")

        # Changes section with file stats
        if diff_stats and diff_stats.get("files", 0) > 0:
            lines.extend(
                (
                    "## Changes",
                    "",
                    f"**{diff_stats['files']}** files changed, "
                    f"**+{diff_stats['insertions']}** insertions, "
                    f"**-{diff_stats['deletions']}** deletions",
                    "",
                )
            )

            # List changed files (limit to 15)
            file_list = diff_stats.get("file_list", [])
            if file_list:
                lines.extend(("<details>", "<summary>Files changed</summary>", ""))
                for f in file_list[:15]:
                    lines.append(f"- `{f}`")
                if len(file_list) > 15:
                    lines.append(f"- ... and {len(file_list) - 15} more")
                lines.extend(("", "</details>", ""))

        # Test results if available
        if test_summary:
            lines.extend(("## Tests", "", test_summary, ""))

        lines.extend(("---", f"*Generated by Bernstein - task `{task.id}`*"))
        return "\n".join(lines)
