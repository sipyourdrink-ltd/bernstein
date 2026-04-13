"""PR cost annotation: post agent run cost summaries as GitHub PR comments.

When Bernstein completes tasks triggered by a PR (review comments, CI fixes),
it can post a summary comment showing the total token cost of that agent run.
This lets maintainers see the AI cost directly in the PR timeline.

Cost comment format:
  > 🤖 **Bernstein agent run cost**
  > Tasks: 3 | Total cost: $0.0042 | Model: claude-sonnet-4-6

All operations degrade gracefully when the ``gh`` CLI is not available or
unauthenticated.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# Template for the cost annotation comment body
_COST_COMMENT_TEMPLATE = """\
<!-- bernstein-cost-annotation -->
> 🤖 **Bernstein agent run cost**
> Tasks completed: {task_count} | Total cost: **${cost_usd:.4f}** | Model: {model}
"""


def post_pr_cost_comment(
    pr_number: int,
    repo: str,
    cost_usd: float,
    task_count: int = 1,
    model: str = "claude-sonnet-4-6",
) -> bool:
    """Post a cost annotation comment on a GitHub pull request.

    Looks for an existing ``<!-- bernstein-cost-annotation -->`` comment and
    updates it in-place if found; otherwise creates a new one.  This keeps
    the PR timeline clean.

    Args:
        pr_number: GitHub PR number.
        repo: ``owner/repo`` slug.
        cost_usd: Total agent run cost in USD.
        task_count: Number of tasks completed in this run.
        model: Model identifier string to show in the annotation.

    Returns:
        ``True`` if the comment was created/updated successfully.
    """
    body = _COST_COMMENT_TEMPLATE.format(
        task_count=task_count,
        cost_usd=cost_usd,
        model=model,
    )

    # Check for an existing cost annotation comment to update
    existing_id = _find_existing_cost_comment(pr_number, repo)
    if existing_id is not None:
        return _update_pr_comment(existing_id, repo, body)
    return _create_pr_comment(pr_number, repo, body)


def build_cost_summary(cost_usd: float, task_count: int, model: str) -> str:
    """Build a markdown cost annotation string without posting it.

    Useful for embedding cost info in check run outputs.

    Args:
        cost_usd: Total cost in USD.
        task_count: Number of tasks completed.
        model: Model identifier.

    Returns:
        Formatted markdown string.
    """
    return _COST_COMMENT_TEMPLATE.format(
        task_count=task_count,
        cost_usd=cost_usd,
        model=model,
    )


def _find_existing_cost_comment(pr_number: int, repo: str) -> int | None:
    """Find the ID of an existing Bernstein cost annotation comment on the PR.

    Args:
        pr_number: GitHub PR number.
        repo: ``owner/repo`` slug.

    Returns:
        Comment ID integer if found, ``None`` otherwise.
    """
    args = [
        "gh",
        "api",
        f"/repos/{repo}/issues/{pr_number}/comments",
        "--jq",
        '[.[] | select(.body | contains("bernstein-cost-annotation")) | .id][0]',
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode == 0:
            stripped = result.stdout.strip()
            if stripped and stripped != "null":
                return int(stripped)
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.debug("Could not search for existing cost comment: %s", exc)
    return None


def _create_pr_comment(pr_number: int, repo: str, body: str) -> bool:
    """Create a new comment on a GitHub PR.

    Args:
        pr_number: PR number.
        repo: ``owner/repo`` slug.
        body: Comment markdown body.

    Returns:
        ``True`` if created successfully.
    """
    payload = json.dumps({"body": body}).encode("utf-8")
    args = [
        "gh",
        "api",
        "--method",
        "POST",
        f"/repos/{repo}/issues/{pr_number}/comments",
        "--input",
        "-",
    ]
    return _run_gh(args, payload)


def _update_pr_comment(comment_id: int, repo: str, body: str) -> bool:
    """Update an existing PR comment in-place.

    Args:
        comment_id: GitHub comment ID.
        repo: ``owner/repo`` slug.
        body: New comment markdown body.

    Returns:
        ``True`` if updated successfully.
    """
    payload = json.dumps({"body": body}).encode("utf-8")
    args = [
        "gh",
        "api",
        "--method",
        "PATCH",
        f"/repos/{repo}/issues/comments/{comment_id}",
        "--input",
        "-",
    ]
    return _run_gh(args, payload)


def _run_gh(args: list[str], stdin: bytes | None = None) -> bool:
    """Run a ``gh`` command with optional stdin input.

    Args:
        args: Full argument list including ``"gh"`` as the first element.
        stdin: Optional bytes to pipe to stdin.

    Returns:
        ``True`` if the command succeeded (exit code 0).
    """
    try:
        result = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "gh command failed (rc=%d): %s",
                result.returncode,
                result.stderr.decode("utf-8", errors="replace").strip(),
            )
            return False
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("gh command error: %s", exc)
        return False


def aggregate_pr_cost(task_costs: list[dict[str, Any]]) -> float:
    """Sum the token costs from a list of task cost dicts.

    Each dict may contain a ``cost_usd`` float key.  Missing keys are treated
    as zero.

    Args:
        task_costs: List of cost dicts from the task store or cost tracker.

    Returns:
        Total cost in USD.
    """
    return sum(float(t.get("cost_usd", 0.0)) for t in task_costs)
