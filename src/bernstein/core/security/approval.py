"""Approval gates: configurable review step between janitor verification and merge.

Three modes:
  auto    — merge immediately after janitor passes (default, headless-friendly).
  review  — write a pending approval file, block until the user writes a decision
             file via ``bernstein approve <task_id>`` or ``bernstein reject <task_id>``.
  pr      — push the agent branch and create a GitHub PR; skip local merge.

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
    deadline = time.monotonic() + max_wait_s
    approved_path = approvals_dir / f"{task_id}.approved"
    rejected_path = approvals_dir / f"{task_id}.rejected"

    while time.monotonic() < deadline:
        if approved_path.exists():
            logger.info("Approval gate: task %s approved via file", task_id)
            return "approved"
        if rejected_path.exists():
            logger.info("Approval gate: task %s rejected via file", task_id)
            return "rejected"
        time.sleep(poll_interval_s)

    logger.warning(
        "Approval gate: task %s timed out after %.0fs — defaulting to %s",
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
        if bypass_enabled:
            logger.info("Approval gate: bypassing review for task %s", task.id)
            return ApprovalResult(approved=True)

        mode = override_mode if override_mode is not None else self._mode
        if mode == ApprovalMode.AUTO:
            return ApprovalResult(approved=True)

        if mode == ApprovalMode.PR:
            return ApprovalResult(approved=False, rejected=False)

        # REVIEW mode
        return self._review(task, session_id=session_id, diff=diff, test_summary=test_summary, timeout_s=timeout_s)

    def create_pr(
        self,
        task: Task,
        *,
        worktree_path: Path,
        session_id: str,
        base_branch: str = "main",
        labels: list[str] | None = None,
        role: str = "",
        model: str = "",
        cost_usd: float = 0.0,
        test_summary: str = "",
    ) -> str:
        """Push the agent branch and open a GitHub PR.

        Pushes the current HEAD of the worktree to ``bernstein/task-{task.id}``
        on the remote (using a refspec so the local branch name is irrelevant),
        then creates a PR with a structured body including task metadata, cost,
        test results, and the agent role/model.

        Args:
            task: The task whose work should become a PR.
            worktree_path: Path to the agent's git worktree.
            session_id: Agent session ID (used for logging).
            base_branch: Target branch for the PR.
            labels: GitHub labels to attach (defaults to ["bernstein", "auto-generated"]).
            role: Agent role that produced this work (e.g. ``"backend"``).
            model: Model name used by the agent (e.g. ``"sonnet"``).
            cost_usd: Approximate cost of the agent run in USD.
            test_summary: One-line test result summary (e.g. ``"12 passed, 0 failed"``).

        Returns:
            PR URL on success, empty string on failure.
        """
        from bernstein.core.git_ops import PullRequestResult, create_github_pr, enable_pr_auto_merge, push_head_as

        effective_labels = labels if labels is not None else self._pr_labels
        pr_branch = f"bernstein/task-{task.id}"

        # Use push_head_as so the local branch name (agent/{session_id}) does
        # not matter — we publish the worktree HEAD as bernstein/task-{id}.
        push_fn: _PushBranchFn = self._push_branch_fn or push_head_as
        create_fn = self._create_pr_fn or create_github_pr

        # Check if the worktree has any commits beyond base before pushing.
        # Prevents "No commits between main and branch" GitHub API errors.
        import subprocess

        try:
            diff_check = subprocess.run(
                ["git", "diff", "--quiet", f"{base_branch}...HEAD"],
                cwd=str(worktree_path),
                capture_output=True,
                timeout=10,
            )
            if diff_check.returncode == 0:
                logger.info(
                    "Approval gate: no diff vs %s for task %s — skipping PR (agent made no changes)",
                    base_branch,
                    task.id,
                )
                return ""
        except (subprocess.TimeoutExpired, OSError):
            pass  # Proceed with push attempt if check fails

        push_result = push_fn(worktree_path, pr_branch)
        if not getattr(push_result, "ok", True):
            stderr = getattr(push_result, "stderr", "")
            logger.warning("Approval gate: push failed for task %s, retrying: %s", task.id, stderr)
            import time as _time

            _time.sleep(2)
            push_result = push_fn(worktree_path, pr_branch)
            if not getattr(push_result, "ok", True):
                logger.error("Approval gate: push failed on retry for task %s", task.id)
                return ""

        # Get diff stats for the PR body
        diff_stats = self._get_diff_stats(worktree_path, base_branch)

        pr_result: PullRequestResult = create_fn(
            cwd=self._workdir,
            title=task.title,
            body=self._pr_body(task, test_summary=test_summary, diff_stats=diff_stats),
            head=pr_branch,
            base=base_branch,
            labels=effective_labels,
        )
        if pr_result.success:
            logger.info("Approval gate: PR created for task %s: %s", task.id, pr_result.pr_url)
            if self._auto_merge and pr_result.pr_url:
                auto_result = enable_pr_auto_merge(self._workdir, pr_result.pr_url)
                if auto_result.ok:
                    logger.info("Approval gate: auto-merge enabled for PR %s", pr_result.pr_url)
                else:
                    logger.warning(
                        "Approval gate: failed to enable auto-merge for PR %s: %s",
                        pr_result.pr_url,
                        auto_result.stderr,
                    )
            return pr_result.pr_url

        logger.warning("Approval gate: PR creation failed for task %s: %s", task.id, pr_result.error)
        return ""

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
            "Approval gate: task %s pending review — run `bernstein approve %s` or `bernstein reject %s`",
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
            lines.append("## Changes")
            lines.append("")
            lines.append(
                f"**{diff_stats['files']}** files changed, "
                f"**+{diff_stats['insertions']}** insertions, "
                f"**-{diff_stats['deletions']}** deletions"
            )
            lines.append("")

            # List changed files (limit to 15)
            file_list = diff_stats.get("file_list", [])
            if file_list:
                lines.append("<details>")
                lines.append("<summary>Files changed</summary>")
                lines.append("")
                for f in file_list[:15]:
                    lines.append(f"- `{f}`")
                if len(file_list) > 15:
                    lines.append(f"- ... and {len(file_list) - 15} more")
                lines.append("")
                lines.append("</details>")
                lines.append("")

        # Test results if available
        if test_summary:
            lines.append("## Tests")
            lines.append("")
            lines.append(test_summary)
            lines.append("")

        lines.append("---")
        lines.append(f"*Generated by Bernstein — task `{task.id}`*")
        return "\n".join(lines)
